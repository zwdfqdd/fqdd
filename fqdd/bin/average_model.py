# Copyright (c) 2020 Mobvoi Inc (Di Wu)
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import json
import os
import argparse
import glob
import sys

import yaml
import torch


def get_args():
    parser = argparse.ArgumentParser(description='average model')
    parser.add_argument('--dst_model', required=True, help='averaged model')
    parser.add_argument('--src_path',
                        required=True,
                        help='src model path for average')
    parser.add_argument('--val_best',
                        action="store_true",
                        help='averaged model')
    parser.add_argument('--num',
                        default=5,
                        type=int,
                        help='nums for averaged model')
    parser.add_argument('--min_epoch',
                        default=0,
                        type=int,
                        help='min epoch used for averaging model')
    parser.add_argument('--max_epoch',
                        default=sys.maxsize,
                        type=int,
                        help='max epoch used for averaging model')

    args = parser.parse_args()
    print(args)
    return args


def main():
    args = get_args()
    checkpoints = []
    val_scores = []
    if args.val_best:
        jsons = glob.glob('{}/epoch_*.json'.format(args.src_path))
        for y in jsons:
            with open(y, 'r') as f:
                dic_json = json.load(f)
                loss = dic_json['result_dict']['loss']
                epoch = dic_json['epoch']
                tag = dic_json['tag']
                if args.min_epoch <= epoch <= args.max_epoch:
                    val_scores += [[epoch, loss, tag]]
        sorted_val_scores = sorted(val_scores,
                                   key=lambda x: x[2],
                                   reverse=False)
        print("best val (epoch, step, loss, tag) = " +
              str(sorted_val_scores[:args.num]))
        path_list = [
            args.src_path + '/{}.pt'.format(score[-1])
            for score in sorted_val_scores[:args.num]
        ]
    else:
        path_list = glob.glob('{}/[!init]*.pt'.format(args.src_path))
        path_list = sorted(path_list, key=os.path.getmtime)
        path_list = path_list[-args.num:]
    print(path_list)
    avg = {}
    num = args.num
    assert num == len(path_list)
    for path in path_list:
        print('Processing {}'.format(path))
        states = torch.load(path, map_location=torch.device('cpu'))
        for k in states.keys():
            if k not in avg.keys():
                avg[k] = states[k].clone()
            else:
                avg[k] += states[k]
    # average
    for k in avg.keys():
        if avg[k] is not None:
            # pytorch 1.6 use true_divide instead of /=
            avg[k] = torch.true_divide(avg[k], num)
    print('Saving to {}'.format(args.dst_model))
    torch.save(avg, args.dst_model)


if __name__ == '__main__':
    main()
