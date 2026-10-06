import torch
import torch.nn as nn

class FNN(nn.Module):
    """
    复刻 2024年 Elsevier Composites Part A 文献中的标准 FNN 算法
    用于 3D 热历程预测
    """
    def __init__(self, args):
        super(FNN, self).__init__()
        # 文献设定：输入 5 维，输出 1 维 (温度)
        # 隐藏层：3层，每层 256 单元
        self.net = nn.Sequential(
            nn.Linear(args.enc_in, 256),
            nn.Tanh(),
            nn.Linear(256, 256),
            nn.Tanh(),
            nn.Linear(256, 256),
            nn.Tanh(),
            nn.Linear(256, args.c_out)
        )

    def forward(self, x_enc, x_mark_enc, x_dec, x_mark_dec):
        # 文献中 FNN 通常处理序列中的最后一个点或展开点
        # 适配你的框架：取序列最后一个时间步进行回归预测
        x = x_enc[:, -1, :]
        return self.net(x).unsqueeze(1).repeat(1, 24, 1) # 适配 pred_len=24

class TgNN_Base_Concordia(nn.Module):
    """
    复刻 2025年 Composites Science and Technology 文献中的 FNN 基准版
    用于与理论引导模型进行对比
    """
    def __init__(self, args):
        super(TgNN_Base_Concordia, self).__init__()
        # 文献设定：1 隐藏层，1024 单元
        self.net = nn.Sequential(
            nn.Linear(args.enc_in, 1024),
            nn.Tanh(),
            nn.Linear(1024, args.c_out)
        )

    def forward(self, x_enc, x_mark_enc, x_dec, x_mark_dec):
        x = x_enc[:, -1, :]
        return self.net(x).unsqueeze(1).repeat(1, 24, 1)