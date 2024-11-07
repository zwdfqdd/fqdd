import os
import json
import torch

from typing import List, Optional, Union, Tuple, Dict


class Tokenizers:

    def __init__(self,
                config,
                token_type: str = "char",
                special_tokens=None,
                unk: str = "<unk>",
                connect_symbol: str = ''
                 ):

        self.unk = unk
        self.dict_path = config["tokenizer_conf"]["symbol_table_path"]
        self.split_with_space = config["tokenizer_conf"]["split_with_space"]
        self.special_tokens = special_tokens
        self.connect_symbol = connect_symbol
        self.ch2ids_dict, self.ids2ch_dict = self.read_file()
        self.blank_id = self.ch2ids_dict["<blank>"]

    def read_file(self):

        print(self.dict_path)
        assert os.path.exists(self.dict_path) is True
        
        ch2ids_dict = {k: int(v) for k, v in [kv.split(" ") for kv in open(self.dict_path, 'r').readlines()]}
        ids2ch_dict = {v: k for k, v in ch2ids_dict.items()}

        return ch2ids_dict, ids2ch_dict

    def text2tokens(self, line: str)-> List[str]:
        line = line.strip()

        parts = [line]

        tokens = []
        for part in parts:
            if self.split_with_space:
                part = part.split(" ")
            for ch in part:
                if ch == ' ':
                    ch = "▁"
                tokens.append(ch)
        return tokens

    def tokens2text(self, tokens: List[str]) -> str:
        return self.connect_symbol.join(tokens)

    def tokens2ids(self, tokens: List[str]) -> List[int]:

        ids = []
        for ch in tokens:
            if ch in self.ch2ids_dict.keys():
                ids.append(self.ch2ids_dict[ch])
            else:
                ids.append(self.ch2ids_dict[self.unk])

        return ids

    def id2tokens(self, ids: List[int]) -> List[str]:
        content = ""
        if len(ids) == 0:
            pass
        else:
            content = [self.ids2ch_dict[w] for w in ids]
        return content

    def vocab_size(self):
        return len(self.ch2ids_dict)


def pad_list(xs: List[torch.Tensor], pad_value: int):
    """Perform padding for the list of tensors.

    Args:
        xs (List): List of Tensors [(T_1, `*`), (T_2, `*`), ..., (T_B, `*`)].
        pad_value (float): Value for padding.

    Returns:
        Tensor: Padded tensor (B, Tmax, `*`).

    Examples:
        >>> x = [torch.ones(4), torch.ones(2), torch.ones(1)]
        >>> x
        [tensor([1., 1., 1., 1.]), tensor([1., 1.]), tensor([1.])]
        >>> pad_list(x, 0)
        tensor([[1., 1., 1., 1.],
                [1., 1., 0., 0.],
                [1., 0., 0., 0.]])

    """
    max_len = max([len(item) for item in xs])
    batchs = len(xs)
    ndim = xs[0].ndim
    if ndim == 1:
        pad_res = torch.zeros(batchs,
                              max_len,
                              dtype=xs[0].dtype,
                              device=xs[0].device)
    elif ndim == 2:
        pad_res = torch.zeros(batchs,
                              max_len,
                              xs[0].shape[1],
                              dtype=xs[0].dtype,
                              device=xs[0].device)
    elif ndim == 3:
        pad_res = torch.zeros(batchs,
                              max_len,
                              xs[0].shape[1],
                              xs[0].shape[2],
                              dtype=xs[0].dtype,
                              device=xs[0].device)
    else:
        raise ValueError(f"Unsupported ndim: {ndim}")
    pad_res.fill_(pad_value)
    for i in range(batchs):
        pad_res[i, :len(xs[i])] = xs[i]
    return pad_res


def add_sos_eos(self, ys_pad: torch.Tensor, sos: int, eos: int,
                ignore_id: int) -> Tuple[torch.Tensor, torch.Tensor]:
    """Add <sos> and <eos> labels.

    Args:
        self (torch.Tensor): batch of padded target sequences (B, Lmax)
        sos (int): index of <sos>
        eos (int): index of <eeos>
        ignore_id (int): index of padding

    Returns:
        ys_in (torch.Tensor) : (B, Lmax + 1)
        ys_out (torch.Tensor) : (B, Lmax + 1)

    Examples:
        >>> sos_id = 10
        >>> eos_id = 11
        >>> ignore_id = -1
        >>> self
        tensor([[ 1,  2,  3,  4,  5],
                [ 4,  5,  6, -1, -1],
                [ 7,  8,  9, -1, -1]], dtype=torch.int32)
        >>> ys_in,ys_out=add_sos_eos(self, sos_id , eos_id, ignore_id)
        >>> ys_in
        tensor([[10,  1,  2,  3,  4,  5],
                [10,  4,  5,  6, 11, 11],
                [10,  7,  8,  9, 11, 11]])
        >>> ys_out
        tensor([[ 1,  2,  3,  4,  5, 11],
                [ 4,  5,  6, 11, -1, -1],
                [ 7,  8,  9, 11, -1, -1]])
    """
    _sos = torch.tensor([sos],
                        dtype=torch.long,
                        requires_grad=False,
                        device=ys_pad.device)
    _eos = torch.tensor([eos],
                        dtype=torch.long,
                        requires_grad=False,
                        device=ys_pad.device)
    ys = [y[y != ignore_id] for y in ys_pad]  # parse padded ys
    ys_in = [torch.cat([_sos, y], dim=0) for y in ys]
    ys_out = [torch.cat([y, _eos], dim=0) for y in ys]

    return pad_list(ys_in, eos), pad_list(ys_out, ignore_id)


'''
tokenizer = Tokenizers("data/train/data.list")
'''

