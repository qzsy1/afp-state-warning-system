import sys
import os
import importlib
import torch
import torch.nn as nn
import torch.nn.functional as F

xju_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if xju_root not in sys.path:
    sys.path.insert(0, xju_root)
current_dir = os.path.dirname(os.path.abspath(__file__))
if current_dir not in sys.path:
    sys.path.insert(1, current_dir)

from shijie import TCN as TCN
from shijie import m_self_attention as attention
from shijie import GAT0 as GAT
from shijie import GCNGRU
from shijie import condition_shift
from shijie import interaction_attention
from modern_TCN_models import modernTCN_ai as ModernTCN

importlib.reload(GCNGRU)
importlib.reload(TCN)
importlib.reload(GAT)
importlib.reload(condition_shift)
importlib.reload(interaction_attention)
importlib.reload(ModernTCN)


# 🚨 彻底动态化配置：接收外部参数，自适应 seq_len 和 pred_len
class ModernTCNConfig:
    def __init__(self, configs):
        self.sensor_n = configs.enc_in
        self.stem_ratio = 2
        self.downsample_ratio = 1
        self.ffn_ratio = 2
        self.num_blocks = [1]
        self.large_size = [7]
        self.small_size = [3]
        self.dims = [64]
        self.dw_dims = [32]

        self.nvars = self.sensor_n
        self.c_in = self.sensor_n
        self.enc_in = self.sensor_n

        self.revin = False
        self.small_kernel_merged = False
        self.dropout = configs.dropout if hasattr(configs, 'dropout') else 0.2
        self.head_dropout = 0.1
        self.use_multi_scale = False

        # 🚨 核心修复：根据传进来的参数动态设定长度！
        self.seq_len = configs.seq_len
        self.pred_len = configs.pred_len
        self.target_window = configs.pred_len  # 强迫 TCN 直接输出预测长度

        self.affine = True
        self.subtract_last = False
        self.freq = None
        self.individual = False
        self.kernel_size = 3
        self.patch_size = 2
        self.patch_stride = 1
        self.decomposition = False


class MechanicalLifetimePredictor(nn.Module):
    def __init__(self, configs, hidden_dim=32, activation=nn.ReLU(),
                 use_ia=True, use_tcn=True, use_gat=True):
        super(MechanicalLifetimePredictor, self).__init__()
        self.sensor_n = configs.enc_in
        self.seq_len = configs.seq_len
        self.pred_len = configs.pred_len
        self.condition_n = 4  # 时间戳的特征数
        self.activation = activation

        self.use_ia = use_ia
        self.use_tcn = use_tcn
        self.use_gat = use_gat

        if self.use_ia:
            self.interactive_attention = interaction_attention.InteractiveAttention_ai(
                sensor_dim=self.sensor_n, condition_dim=self.condition_n, hidden_dim=hidden_dim
            )
        else:
            self.baseline_fusion = nn.Linear(self.sensor_n + self.condition_n, self.sensor_n)

        if self.use_tcn:
            self.modern_tcn = ModernTCN.Model(ModernTCNConfig(configs))
        else:
            self.baseline_tcn = nn.Sequential(
                nn.Conv1d(self.sensor_n, self.sensor_n, 3, 1, 1),
                self.activation,
                nn.Conv1d(self.sensor_n, self.sensor_n, 3, 1, 1),
                nn.Upsample(size=self.pred_len, mode='linear', align_corners=False)  # 自适应输出长度
            )

        if self.use_gat:
            # 🚨 GAT 自适应：输入特征长等于 pred_len，输出也是 pred_len
            self.gat1 = GAT.GAT(
                feature=self.pred_len, out_channel=self.pred_len,
                pooltype='None'
            )
            source_nodes = torch.arange(self.sensor_n).repeat_interleave(self.sensor_n)
            target_nodes = torch.arange(self.sensor_n).repeat(self.sensor_n)
            base_edge_index = torch.stack([source_nodes, target_nodes], dim=0)
            self.register_buffer('base_edge_index', base_edge_index)
        else:
            self.baseline_gat = nn.Linear(self.pred_len, self.pred_len)

    def forward(self, x, condition, edge_index_dummy, labels_all):
        batch_size = x.size(0)

        # ========================================================
        # 🚨 [内置时序缩放机制]
        # ========================================================
        seq_last = x[:, -1:, :].detach()  # [B, 1, channels]
        x_shifted = x - seq_last
        stdev = torch.sqrt(torch.var(x_shifted, dim=1, keepdim=True, unbiased=False) + 1e-5).detach()
        x_norm = x_shifted / stdev
        # x_norm=x
        # 确保 condition 形状是 [B, seq_len, 4]
        if condition.size(-1) != self.condition_n:
            condition = condition.permute(0, 2, 1)

        # ---------------- 阶段 1：特征融合 ----------------
        if self.use_ia:
            y = self.interactive_attention(x_norm, condition)
        else:
            concat_feat = torch.cat([x_norm, condition], dim=-1)
            y = self.baseline_fusion(concat_feat)

        # ---------------- 阶段 2：时间序列提取 ----------------
        if self.use_tcn:
            tcn_out = self.modern_tcn(y, None, None, None)  # 完美自适应输出 [B, pred_len, channels]
        else:
            tcn_out = self.baseline_tcn(y.permute(0, 2, 1)).permute(0, 2, 1)

        # ---------------- 阶段 3：空间图结构提取 ----------------
        tcn_out_permuted = tcn_out.permute(0, 2, 1)  # 转换为 [B, channels, pred_len]

        if self.use_gat:
            num_edges = self.base_edge_index.size(1)
            edge_index_batch = self.base_edge_index.repeat(1, batch_size)
            offsets = torch.arange(batch_size, device=x.device).repeat_interleave(num_edges) * self.sensor_n
            edge_index_batch = edge_index_batch + offsets

            correct_batch_vec = torch.arange(batch_size, device=x.device).repeat_interleave(self.sensor_n)
            gat_input = tcn_out_permuted.reshape(-1, self.pred_len)  # 展平进行 GAT 处理

            gat1_out, up, low = self.gat1(gat_input, edge_index_batch, correct_batch_vec, pooltype='None')

            # 重塑并恢复维度顺序 -> [B, pred_len, channels]
            aggregated = gat1_out.reshape(batch_size, self.sensor_n, self.pred_len).permute(0, 2, 1)
            if up is not None and low is not None:
                up = up.reshape(batch_size, self.sensor_n, self.pred_len).permute(0, 2, 1)
                low = low.reshape(batch_size, self.sensor_n, self.pred_len).permute(0, 2, 1)
            else:
                up, low = None, None
        else:
            gat1_out = self.baseline_gat(tcn_out_permuted)
            aggregated = gat1_out.permute(0, 2, 1)
            up, low = None, None

        # ========================================================
        # 🚨 [还原物理尺度] 自动广播到 pred_len 长度
        # ========================================================
        aggregated = aggregated * stdev + seq_last
        if up is not None:
            up = up * stdev + seq_last
        if low is not None:
            low = low * stdev + seq_last

        return aggregated, up, low


# ==========================================
# Wrappers
# ==========================================
device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')


def build_dynamic_model(configs, hidden_dim=32, activation=nn.ReLU(), use_ia=True, use_tcn=True, use_gat=True):
    return MechanicalLifetimePredictor(
        configs, hidden_dim=hidden_dim, activation=activation,
        use_ia=use_ia, use_tcn=use_tcn, use_gat=use_gat
    )


class Model_Full(nn.Module):
    def __init__(self, configs):
        super().__init__()
        self.core = MechanicalLifetimePredictor(configs, use_ia=True, use_tcn=True, use_gat=True).to(device)

    def forward(self, batch_x, batch_x_mark, dec_inp, batch_y_mark):
        res = self.core(batch_x, batch_x_mark, None, None)
        return res[0] if isinstance(res, tuple) else res


class Model_wo_IA(nn.Module):
    def __init__(self, configs):
        super().__init__()
        self.core = MechanicalLifetimePredictor(configs, use_ia=False, use_tcn=True, use_gat=True).to(device)

    def forward(self, batch_x, batch_x_mark, dec_inp, batch_y_mark):
        res = self.core(batch_x, batch_x_mark, None, None)
        return res[0] if isinstance(res, tuple) else res


class Model_wo_TCN(nn.Module):
    def __init__(self, configs):
        super().__init__()
        self.core = MechanicalLifetimePredictor(configs, use_ia=True, use_tcn=False, use_gat=True).to(device)

    def forward(self, batch_x, batch_x_mark, dec_inp, batch_y_mark):
        res = self.core(batch_x, batch_x_mark, None, None)
        return res[0] if isinstance(res, tuple) else res


class Model_wo_GAT(nn.Module):
    def __init__(self, configs):
        super().__init__()
        self.core = MechanicalLifetimePredictor(configs, use_ia=True, use_tcn=True, use_gat=False).to(device)

    def forward(self, batch_x, batch_x_mark, dec_inp, batch_y_mark):
        res = self.core(batch_x, batch_x_mark, None, None)
        return res[0] if isinstance(res, tuple) else res


class Model_Baseline(nn.Module):
    def __init__(self, configs):
        super().__init__()
        self.core = MechanicalLifetimePredictor(configs, use_ia=False, use_tcn=False, use_gat=False).to(device)

    def forward(self, batch_x, batch_x_mark, dec_inp, batch_y_mark):
        res = self.core(batch_x, batch_x_mark, None, None)
        return res[0] if isinstance(res, tuple) else res


Model = Model_Full
