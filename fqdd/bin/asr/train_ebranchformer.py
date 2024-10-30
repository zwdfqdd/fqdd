import torch
import torch.nn as nn
import os, sys
import numpy as np
import logging
import json
sys.path.insert(0, "./")

import torch.distributed as dist
from torch.nn.parallel import DistributedDataParallel as DDP
from tqdm import tqdm
from fqdd.prepare_data.asd_data import prepare_data_json
from fqdd.utils.lang import create_phones, read_phones
from fqdd.utils.load_data import Load_Data
from fqdd.utils.argument import parse_arguments
from fqdd.bin.asr.decode import GreedyDecoder, calculate_cer
# from fqdd.models.wav2vec import Encoder_Decoer
from fqdd.models.ebranchformer.ebranchformer import EBranchformer
from fqdd.models.check_model import model_init, save_model, reload_model
from fqdd.utils.optimizers import adam_optimizer, sgd_optimizer, scheduler, warmup_lr
from fqdd.utils.logger import init_logging
from fqdd.nnets.losses import nll_loss, transducer_loss
from fqdd.utils.edit_distance import Static_Info


os.environ["CUDA_VISIBLE_DEVICES"] = "0,1,2"

def calculate_loss(ctc_loss, pred, gold, input_lengths, target_lengths):

    # print("{}\t{}\t{}\t{}".format(pred.shape, gold.shape, input_lengths.shape, target_lengths.shape))
    """
    Calculate loss
    args:
        pred: B x T x C
        gold: B x T
        input_lengths: B (for CTC)
        target_lengths: B (for CTC)
        smoothing:
        type: ce|ctc (ctc => pytorch 1.0.0 or later)
        input_lengths: B (only for ctc)
        target_lengths: B (only for ctc)
    """
    batch = pred.size(0)
    input_lengths = (input_lengths * pred.shape[1]).int()
    target_lengths = (target_lengths * gold.shape[1]).int()
    log_probs = pred.transpose(0, 1)  # T x B x C
    # print(gold.size())
    targets = gold
    # targets = gold.contiguous().view(-1)  # (B*T)

    """
    log_probs: torch.Size([209, 8, 3793])
    targets: torch.Size([8, 46])
    input_lengths: torch.Size([8])
    target_lengths: torch.Size([8])
    """
    
    # log_probs = F.log_softmax(log_probs, dim=2)
    # log_probs = log_probs.detach().requires_grad_()
    loss = ctc_loss(log_probs.to("cpu"), targets.to("cpu"), input_lengths.to("cpu"), target_lengths.to("cpu"))
    loss = loss / batch
    return loss


def train(model, load_object, args, phones, logger):

    min_loss = 1000
    lr_cer = 100
    slice_len = 5    
    epoch_n = args.epoch_n
    device = model.device
    train_sampler = load_object.train_sampler
    train_load = load_object.train_load
    dev_load = load_object.dev_load

    ctc_loss = torch.nn.CTCLoss(blank=0, reduction='mean') 
    optimizer = adam_optimizer(model, args.lr)
    #optimizer = sgd_optimizer(model, args.lr)
    warm_up = warmup_lr(args.lr, 10000)
    # scheduler_lr = scheduler(optimizer, patience=0, cooldown=0)
    
    if args.pretrained:
        # if args.local_rank < 1:
        start_epoch = reload_model(os.path.join(args.result_dir, str(args.seed), "save", "AM"), model=model, optimizer=optimizer, map_location='cuda:{}'.format(args.local_rank))
        #else:
            #start_epoch = reload_model(os.path.join(args.result_dir, str(args.seed), "save", "AM"))
        start_epoch = start_epoch + 1
    else:
        start_epoch = 1
 
    if args.local_rank < 1: 
        logger.info("init_lr:{}".format(optimizer.state_dict()['param_groups'][0]['lr']))
    accum_grad = 4
    for epoch in range(start_epoch, epoch_n+1):
        model.train()
        
        if args.is_distributed:
            train_sampler.set_epoch(epoch)
        if args.local_rank < 1:
            logger.info("Epoch {}/{}".format(epoch, epoch_n))
            logger.info("-" * 10)
        static = Static_Info()
        inter_cers = 0.0
        inter_num = 100
        if args.local_rank < 1:
            print("status: train\t train_load_size:{}".format(len(train_load)))
        dist.barrier() # 同步训练进程
        for idx, data in enumerate(tqdm(train_load)):
            # 只做推理，代码不会更新模型状态 
            feats, targets, targets_bos, targets_eos, wav_lengths, target_lens, target_os_lens = [item.to(device) for item in data]
            
            info_dicts = model(feats, wav_lengths, targets, target_lens) 
            loss = info_dicts["loss"]
            loss.backward()
            
            output_en = info_dicts["encoder_out"]

            targ, pred = GreedyDecoder(output_en, targets, wav_lengths, target_lens, phones)
            cer = static.one_iter(targ, pred, loss)
            inter_cers += cer
            torch.nn.utils.clip_grad_norm_((p for p in model.parameters()), max_norm=50)
            optimizer.step() 
            optimizer.zero_grad()
            warm_up(optimizer)
 
            if (idx+1) % inter_num == 0:
                avg_inter_loss = static.get_inter_loss(inter_num)
                avg_inter_cer = inter_cers / inter_num
                inter_cers = 0.0
                logger.info("batchs:{}/{}   Loss:{:.2f}   CER:{:.2f}".format(epoch, idx+1, avg_inter_loss, avg_inter_cer))
            # torch.distributed.barrier()
            torch.cuda.empty_cache() 
        cer, corr, det, ins, sub = static.avg_one_epoch_cer()
        avg_one_epoch_loss = static.avg_one_epoch_loss(len(train_load))
 
        #scheduler_lr.step(loss)
        if args.local_rank < 1:
            logger.info("Epoch:{}, loss:{}, cer:{}, lr:{}, corr:{}, det:{}, ins:{}, sub:{}".format(epoch, avg_one_epoch_loss, cer, optimizer.state_dict()['param_groups'][0]['lr'], corr, det, ins, sub))
            save_model(model, optimizer, epoch, os.path.join(args.result_dir, str(args.seed), 'save', 'AM'))
        dist.barrier() # 同步测试进程
        with torch.no_grad():
            loss, cer, corr, det, ins, sub = evaluate(model, dev_load, ctc_loss, args, phones, device)
            logger.info("Epoch:{}, DEV:  loss:{}, cer:{}, corr:{}, det:{}, ins:{}, sub:{}".format(epoch, loss, cer, corr, det, ins, sub))
            
    
def evaluate(model, eval_load, ctc_loss, args, phones, device):
    
    model.eval()
    dev_cer = Static_Info()

    for idx, data in enumerate(tqdm(eval_load)):
        feats, targets, targets_bos, targets_eos, wav_lengths, target_lens, target_os_lens = [item.to(device) for item in data]
        
        # print(feats.shape)
        info_dicts = model(feats, wav_lengths, targets, target_lens)
        loss = info_dicts["loss"]
        output_en = info_dicts["encoder_out"]
        
        targ, pred = GreedyDecoder(output_en, targets, wav_lengths, target_lens, phones) 
        cer = dev_cer.one_iter(targ, pred, loss)
        torch.cuda.empty_cache()
    avg_dev_loss = dev_cer.avg_one_epoch_loss(len(eval_load))
    cer, corr, det, ins, sub = dev_cer.avg_one_epoch_cer()
    return avg_dev_loss, cer, corr, det, ins, sub

def main():

    args = parse_arguments()
    torch.manual_seed(args.seed)
    torch.cuda.manual_seed_all(args.seed)
    np.random.seed(args.seed)
    
    '''
    logging.basicConfig(level=logging.DEBUG,
        format='%(asctime)s %(levelname)s %(message)s')
    

    parser = argparse.ArgumentParser(description='extract CMVN stats')
    parser.add_argument('--num_workers',
                        default=0,
                        type=int,
                        help='num of subprocess workers for processing')
    parser.add_argument('--train_config',
                        default='',
                        help='training yaml conf')
    in_args = parser.parse_args()
    '''

    configs = json.load(open(args.config, 'r', encoding="utf-8"))

    # prepare data
    dirpath = os.path.join(args.result_dir, str(args.seed))
    prepare_data_json(args.data_folder, dirpath)
    phones = create_phones(dirpath)

    logger = init_logging("train", dirpath)

    output_dim = len(phones) 
    configs["model"]["vocab_size"] = output_dim
    model_conf = configs["model"]
    
    # model = Encoder_Decoer(output_dim, feat_shape=[args.batch_size, args.max_during*100, input_dim], output_size=1024)
    model = EBranchformer(model_conf)

    if torch.cuda.is_available():
        torch.backends.cudnn.enabled = True
        torch.backends.cudnn.deterministic = True
        torch.backends.cudnn.benchmark = True

        if args.is_distributed:
            torch.cuda.set_device(args.local_rank)
            num_gpus = torch.cuda.device_count()
            # world_size #表示开启的全局进程个数进程数，node * num_gpus_one_node， 
            # rank 表示为第几个节点进程，一般rank=0表示，master节点
            # local_rank: 进程内，GPU 编号，非显式参数，由 torch.distributed.launch 内部指定。比方说， rank = 3，local_rank = 0 表示第 3 个进程内的第 1 块 GPU
            # dist.init_process_group(backend='nccl', init_method=args.host, rank=args.local_rank, world_size=args.world_size)
            dist.init_process_group(backend='nccl')
            device = torch.device('cuda', args.local_rank)
            model.to(device)
            print(num_gpus, device)
            model = DDP(model, device_ids=[args.local_rank], output_device=args.local_rank, find_unused_parameters=True)
    else:
        model = model.to("cpu")
    
    logger.info("\nresult_path:{}\nfeat_type:{}\nfeat_cof:{}\ndevice:{}\nbatch_size:{}\nclassify_num:{}\n"
            .format(dirpath, args.feat_type, args.feat_cof, model.device, args.batch_size, output_dim))
    logger.info(model)

    model_init(model, init_method="kaiming")
    load_object = Load_Data(phones, args)
    load_object.dataload()
 
    train(model, load_object, args, phones, logger)
    # Tear down the process group
    dist.destroy_process_group()

if __name__ == "__main__":
    main()