import torch
import torch.nn as nn
import sys

sys.path.append(r"F:\program\XJUsorceopen")  # 保持你原来的路径
from shijie.model_mine import I_modernTCN_GAT_abalation as train_model


# ====================================================
# 核心基类：接收不同的消融参数并构建模型
# ====================================================
class BaseModel(nn.Module):
    def __init__(self, configs, use_ia=True, use_tcn=True, use_gat=True):
        super(BaseModel, self).__init__()

        h_dim = getattr(configs, 'hidden_dim', getattr(configs, 'd_model', 64))
        act_str = getattr(configs, 'activation', 'relu').lower()
        if act_str == 'gelu':
            act_fn = nn.GELU()
        elif act_str == 'tanh':
            act_fn = nn.Tanh()
        else:
            act_fn = nn.ReLU()

        # 🚨 核心修改：将消融开关正式传给你的 build_dynamic_model
        self.core_model = train_model.build_dynamic_model(
            hidden_dim=h_dim,
            activation=act_fn,
            use_ia=use_ia,
            use_tcn=use_tcn,
            use_gat=use_gat
        )

    def forward(self, x_enc, x_mark_enc, x_dec, x_mark_dec,
                enc_self_mask=None, dec_self_mask=None, dec_enc_mask=None):

        B, L, C = x_enc.shape
        device = x_enc.device

        inputs = torch.cat([x_enc[:, :, 16:], x_enc[:, :, :11]], dim=-1)
        condition = x_enc[:, :, 12:16].permute(0, 2, 1)

        num_sensors = inputs.shape[-1]
        src = torch.arange(num_sensors).repeat_interleave(num_sensors)
        dst = torch.arange(num_sensors).repeat(num_sensors)
        edge_index_dummy = torch.stack([src, dst], dim=0).to(device)

        labels_all = ['dummy_label'] * B

        res = self.core_model(inputs, condition, edge_index_dummy, labels_all)

        if isinstance(res, tuple):
            outputs = res[0]
        else:
            outputs = res

        outputs = outputs.unsqueeze(1)
        pred_len = outputs.shape[1]
        padded_outputs = torch.zeros((B, pred_len, 17), device=device)

        padded_outputs[:, :, 16] = outputs[:, :, 0]
        padded_outputs[:, :, :11] = outputs[:, :, 1:]
        return padded_outputs


# ====================================================
# 衍生出你的 5 种消融实验模型！
# ====================================================
class Model_Full(BaseModel):
    def __init__(self, configs): super().__init__(configs, use_ia=True, use_tcn=True, use_gat=True)


class Model_wo_IA(BaseModel):
    def __init__(self, configs): super().__init__(configs, use_ia=False, use_tcn=True, use_gat=True)


class Model_wo_TCN(BaseModel):
    def __init__(self, configs): super().__init__(configs, use_ia=True, use_tcn=False, use_gat=True)


class Model_wo_GAT(BaseModel):
    def __init__(self, configs): super().__init__(configs, use_ia=True, use_tcn=True, use_gat=False)


class Model_Baseline(BaseModel):
    def __init__(self, configs): super().__init__(configs, use_ia=False, use_tcn=False, use_gat=False)


# 兼容你原来的 i_T_G 名字 (默认等同于 Full)
class Model(Model_Full):
    pass