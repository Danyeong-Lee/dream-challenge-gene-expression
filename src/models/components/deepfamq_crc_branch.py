from typing import List
import math
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from einops import rearrange
from einops.layers.torch import Rearrange


class ConvBlock(nn.Module):
    def __init__(
        self,
        input_dim: int = 4,
        out_dim: int = 320,
        kernel_size: int = 15,
        pool_size: int = 3,
        dropout: float = 0.2,
    ):
        super().__init__()
        self.main = nn.Sequential(
            nn.Conv1d(in_channels=input_dim, out_channels=out_dim, kernel_size=kernel_size, padding="same"),
            nn.ReLU(),
            nn.MaxPool1d(pool_size),
            nn.Dropout(dropout)
        )
    
    def forward(self, x):
        # x: (N, C, L)
        
        return self.main(x)
    

class MLPBlock(nn.Module):
    def __init__(
        self,
        input_dim,
        hidden_dim: int = 64
    ):
        super().__init__()
        self.main = nn.Sequential(
            nn.Linear(input_dim, hidden_dim),
            nn.ReLU(),
            nn.Linear(hidden_dim, hidden_dim),
            nn.ReLU()
        )
    
    def forward(self, x):
        return self.main(x)


class DeepFamQ_CRC(nn.Module):
    def __init__(
        self,
        conv_out_dim: int = 320,
        conv_kernel_size: List = [9, 15],
        pool_size: int = 1,
        lstm_hidden_dim: int = 320,
        fc_hidden_dim: int = 64,
        dropout1: float = 0.2,
        dropout2: float = 0.5
    ):
        super().__init__()
        
        conv_each_dim = int(conv_out_dim / len(conv_kernel_size))
        self.conv_blocks1 = nn.ModuleList([ConvBlock(4, conv_each_dim, k, pool_size, dropout1) for k in conv_kernel_size])
        self.c_mlp = nn.Sequential(
            Rearrange("N C L -> N (L C)"),
            MLPBlock(conv_out_dim * 110, fc_hidden_dim)
        )
        
        self.lstm = nn.LSTM(input_size=conv_out_dim, hidden_size=lstm_hidden_dim, bidirectional=True, batch_first=True)
        self.cr_mlp = nn.Sequential(
            Rearrange("N L C -> N (L C)"),
            nn.Dropout(dropout2),
            MLPBlock(lstm_hidden_dim * 2 * 110, fc_hidden_dim)
        )
        
        conv_each_dim = int(lstm_hidden_dim / len(conv_kernel_size))
        self.conv_blocks2 = nn.ModuleList([ConvBlock(lstm_hidden_dim * 2, conv_each_dim, k, pool_size, dropout1) for k in conv_kernel_size])
        self.crc_mlp = nn.Sequential(
            Rearrange("N C L -> N (L C)"),
            nn.Dropout(dropout2),
            MLPBlock(lstm_hidden_dim * 110, fc_hidden_dim)
        )
        
        self.final = nn.Linear(fc_hidden_dim, 1)
        
    def forward(self, x):
        x = rearrange(x, "N L C -> N C L")
        
        conv_outs = []
        for conv in self.conv_blocks1:
            conv_outs.append(conv(x))
        x = torch.cat(conv_outs, dim=1)
        c_embed = self.c_mlp(x)
        c_out = self.final(c_embed)
        
        x = rearrange(x, "N C L -> N L C")
        x, (h, c) = self.lstm(x)
        cr_embed = self.cr_mlp(x)
        cr_out = self.final(cr_embed)
        
        x = rearrange(x, "N L C -> N C L")
        
        conv_outs = []
        for conv in self.conv_blocks2:
            conv_outs.append(conv(x))
        x = torch.cat(conv_outs, dim=1)
        crc_embed = self.crc_mlp(x)
        crc_out = self.final(crc_embed)
        
        comb_embed = (c_embed + cr_embed + crc_embed) / 3
        comb_out = self.final(comb_embed)
        
        return c_out.squeeze(), cr_out.squeeze(), crc_out.squeeze(), comb_out.squeeze()
    
