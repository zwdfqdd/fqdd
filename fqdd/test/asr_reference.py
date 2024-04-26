import os
import torch.nn
from script.tools.argument import parse_arguments
from script.tools.feature import get_feats
from script.asr.decode import decoder
import script.model.CRDNN as CRDNN
from script.tools.lang import read_phones
from script.tools.load_data import load_data
from apex import amp


def load_model(args, device):
    try:
        dic = read_phones(args.phones_path)
        if args.feat_type is 'mfcc':
            input_dim = args.feat_cof * 3
        elif args.feat_type is 'fbank':
            input_dim = args.feat_cof
        output_dim = len(dic)
        print(output_dim)

        model =  Encoder_Decoer(output_dim, feat_shape=[args.batch_size, args.max_during*100, input_dim])
        model.to(device)

        checkpoint = torch.load(args.reload_asr_model)
        model.load_state_dict(checkpoint['model'])
    except:
        print("set correct model_path")
    finally:
        return model.eval()

    dic = read_phones(args.phones_path)
    if os.path.exists(args.decode_path):
        get_feats(args.decode_path, args.feat_save_path + '/test', feat_type=args.feat_type, feat_cof=args.feat_cof)
    test_load = load_data(args.feat_save_path + '/test', args, shuffle=False)
    for idx, data in enumerate(test_load):
        x_train, y_train, wavlist_length, label_length = data
        x_train.to(device)
        pre = model(x_train)
        pre_strs = decoder(pre, dic)
        print(pre_strs)


def main():
    device = torch.device('cuda:0') if torch.cuda.is_available() else torch.device('cpu')
    args = parse_arguments()
    model, amp = load_model(args, device)
    inference(model, args, device)


if __name__ == '__main__':
    main()
