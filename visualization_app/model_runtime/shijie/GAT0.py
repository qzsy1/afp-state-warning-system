import torch
import torch.nn.functional as F
import torch.nn as nn
from torch_geometric.nn import GATConv,  BatchNorm # noqa
from torch_geometric.nn import TopKPooling,  EdgePooling, ASAPooling, SAGPooling, global_mean_pool

class GAT(torch.nn.Module):
    def __init__(self, feature, out_channel,pooltype='EdgePooling',hid_channel=256,target_nodes=64):
        super(GAT, self).__init__()
        self.edge_index = None
        # hid_channel=256
        # self.pool1, self.pool2 = self.poollayer(pooltype)
        self.GConv1 = GATConv(feature,hid_channel)
        self.bn1 = BatchNorm(hid_channel)

        self.GConv2 = GATConv(hid_channel,hid_channel)
        self.bn2 = BatchNorm(hid_channel)

        self.fc = nn.Sequential(nn.Linear(hid_channel, hid_channel//2), nn.ReLU(inplace=True))
        self.dropout = nn.Dropout(0.2)
        self.fc1 = nn.Sequential(nn.Linear(hid_channel//2, out_channel))
        self.fc2 = nn.Sequential(nn.Linear(hid_channel//2, out_channel))
        self.fc3 = nn.Sequential(nn.Linear(64, 14), nn.ReLU(inplace=True))

    def forward(self, x,edge_index,batch,pooltype='EdgePool'):
        attn_weights = []
        # x, edge_index, batch= data.x, data.edge_index, data.batch
        edge_index = edge_index.clone().detach()
        # print('edge_index_gatin:', edge_index)
        # x = self.GConv1(x, edge_index)
        x, (edge_index1, weight1) = self.GConv1(x, edge_index, return_attention_weights=True)
        attn_weights.append((edge_index1, weight1))
        # print(edge_index1.size(), weight1.size())
        # print('x_Gat',x.shape)
        x = self.bn1(x)
        x = F.relu(x)
        # print(x.shape)
        # print(batch)
        # print(len(batch))
        # print(edge_index.size)
        # un,n=torch.unique(batch,return_counts=True)
        # dupli=un[n>1]
        # for i in dupli:
        #     n = (batch == i).sum()
        #     # print(n)
        #     # print(batch)
        #     #
        #     batch[:n] = batch[:n] + torch.tensor([i for i in range(n - 1, -1, -1)]).to(x.device)
        batch = self.remap_batch(batch)  # 使用优化后的重映射

        # 【修改点】使用 getattr 安全获取 pool1，如果内存中找不到 self.pool1，则默认给 None
        # 这样能完全避开 AttributeError 报错
        safe_pool1 = getattr(self, 'pool1', None)
        x, edge_index, batch1 = self.poolresult(safe_pool1, pooltype, x, edge_index, batch)
        # print('pool',x.shape)
        x, (edge_index2, weight2) = self.GConv2(x, edge_index, return_attention_weights=True)
        # print(len(batch))
        batch = self.remap_batch(batch)
        # print(len(batch))
        # print(edge_index2.size(), weight2.size())
        # print(weight2)
        attn_weights.append((edge_index2, weight2))
        # print('x_Gat', x.shape)


        x = self.bn2(x)
        x = F.relu(x)

        # 【修改点】同样使用 getattr 安全获取 pool2
        safe_pool2 = getattr(self, 'pool2', None)
        x, edge_index, batch2 = self.poolresult(safe_pool2, pooltype, x, edge_index, batch)
        x = self.fc(x)
        x = self.dropout(x)
        up = self.fc1(x)
        low = self.fc2(x)
        x=(up+low)/2
        # print(x.shape)
        # x=self.fc3(x.permute(2,1,0))
        # print(x.shape)
        return x,up,low
    # def forward(self, x, edge_index, batch, pooltype='EdgePool'):
    #     print(batch)
    #     attn_weights = []
    #     edge_index = torch.tensor(edge_index).to(x.device).to(torch.long)
    #
    #     # 记录原始 batch 的唯一值数量
    #     original_batch_count = torch.unique(batch).size(0)
    #     print(f"原始 batch 数量: {original_batch_count}")
    #
    #     # 第一层 GAT
    #     x, (edge_index1, weight1) = self.GConv1(x, edge_index, return_attention_weights=True)
    #     attn_weights.append((edge_index1, weight1))
    #     x = self.bn1(x)
    #     x = F.relu(x)
    #
    #     # 第一次池化
    #     x, edge_index, batch = self.poolresult(self.pool1, pooltype, x, edge_index, batch)
    #     print(f"第一次池化后节点数: {x.size(0)}, batch 唯一值: {torch.unique(batch)}")
    #
    #     # 重置 batch 索引
    #     batch = self.remap_batch(batch)
    #     print(f"重置后 batch 唯一值: {torch.unique(batch)}, 数量: {torch.unique(batch).size(0)}")
    #
    #     x1 = global_mean_pool(x, batch)
    #     print(f"第一次全局池化后: {x1.shape}")
    #
    #     # 第二层 GAT
    #     x, (edge_index2, weight2) = self.GConv2(x, edge_index, return_attention_weights=True)
    #     attn_weights.append((edge_index2, weight2))
    #     x = self.bn2(x)
    #     x = F.relu(x)
    #
    #     # 第二次池化
    #     x, edge_index, batch = self.poolresult(self.pool2, pooltype, x, edge_index, batch)
    #     print(f"第二次池化后节点数: {x.size(0)}, batch 唯一值: {torch.unique(batch)}")
    #
    #     # 再次重置 batch 索引
    #     batch = self.remap_batch(batch)
    #     print(f"重置后 batch 唯一值: {torch.unique(batch)}, 数量: {torch.unique(batch).size(0)}")
    #
    #     x2 = global_mean_pool(x, batch)
    #     print(f"第二次全局池化后: {x2.shape}")
    #
    #     # 合并特征
    #     x = x1 + x2
    #     x = self.fc(x)
    #     x = self.dropout(x)
    #     up = self.fc1(x)
    #     low = self.fc2(x)
    #     x = (up + low) / 2
    #     print(f"最终输出: {x.shape}")
    #     return x, up, low
    def remap_batch(self, batch):
        # 确保 batch 是整数类型
        batch = batch.long()

        # 获取唯一值并排序
        unique_vals, inverse = torch.unique(batch, sorted=True, return_inverse=True)

        # 直接使用 inverse 作为新的 batch 索引
        return inverse

    def poollayer(self, pooltype):
        self.pooltype = pooltype
        # 先在开头给它们一个默认的 None，防止走丢导致报错
        self.pool1 = None
        self.pool2 = None

        if self.pooltype == 'TopKPool':
            self.pool1 = TopKPooling(1024)
            self.pool2 = TopKPooling(1024)
        elif self.pooltype == 'EdgePool':
            self.pool1 = EdgePooling(1024)
            self.pool2 = EdgePooling(1024)
        elif self.pooltype == 'ASAPool':
            self.pool1 = ASAPooling(1024)
            self.pool2 = ASAPooling(1024)
        elif self.pooltype == 'SAGPool':
            self.pool1 = SAGPooling(1024)
            self.pool2 = SAGPooling(1024)
        elif pooltype == 'None' or pooltype is None:
            self.pool1 = None  # 👈 显式赋值，代替 pass
            self.pool2 = None  # 👈 显式赋值
        else:
            print(f'Warning: Graph pool method {pooltype} is not implemented! Using None.')
            self.pool1 = None  # 兜底赋值
            self.pool2 = None

        return self.pool1, self.pool2

    def poolresult(self, pool, pooltype, x, edge_index, batch):
        # 加入拦截：如果池化层是 None，直接跳过计算，原样返回节点
        if pool is None or pooltype == 'None':
            return x, edge_index, batch

        self.pool = pool

        if pooltype == 'TopKPool':
            x, edge_index, batch, _= self.pool(x=x, edge_index=edge_index, batch=batch)
        elif pooltype == 'EdgePool':
            x, edge_index, batch, _  = self.pool(x=x, edge_index=edge_index, batch=batch)
        elif pooltype == 'ASAPool':
            x, edge_index, _, batch, _ = self.pool(x=x, edge_index=edge_index, batch=batch)
        elif pooltype == 'SAGPool':
            x, edge_index, _, batch, _, _ = self.pool(x=x, edge_index=edge_index, batch=batch)
        else:
            print('Such graph pool method is not implemented!!')

        return x, edge_index, batch


