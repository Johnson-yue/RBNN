import torch
import torch.nn as nn
import math
import numpy as np
import torch.nn.functional as F
from torch.autograd import Function,Variable
from scipy.stats import ortho_group
from utils.options import args

class BinarizeConv2d(nn.Conv2d):

    def __init__(self, *kargs, **kwargs):
        super(BinarizeConv2d, self).__init__(*kargs, **kwargs)
        self.k = torch.tensor([10.]).float()
        self.t = torch.tensor([0.1]).float()
        self.epoch=-1

        w=self.weight
        self.a,self.b=get_ab(np.prod(w.shape[1:]))
        R1=torch.tensor(ortho_group.rvs(dim=self.a)).float().cuda()
        R2=torch.tensor(ortho_group.rvs(dim=self.b)).float().cuda()
        self.register_buffer('R1', R1)
        self.register_buffer('R2', R2)
        self.Rweight=torch.ones_like(w)

        sw = w.abs().view(w.size(0), -1).mean(-1).float().view(w.size(0), 1, 1).detach()
        self.alpha=nn.Parameter(sw.cuda(),requires_grad=True)
        self.rotate = nn.Parameter(torch.ones(w.size(0),1,1,1).cuda()*np.pi/2,requires_grad=True)
        self.Rotate = torch.zeros(1)

    def forward(self, input):
        a0 = input
        w = self.weight
        w1 = w - w.view(w.size(0), -1).mean(-1).view(w.size(0), 1, 1, 1)
        w2 = w1 / w1.view(w1.size(0), -1).std(-1).view(w1.size(0), 1, 1, 1) 
        if args.ba:
            a1 = a0 - a0.view(a0.size(0), -1).mean(-1).view(a0.size(0), 1, 1, 1)
            a2 = a1 / a1.view(a1.size(0), -1).std(-1).view(a1.size(0), 1, 1, 1) 
        else: 
            a2 = a0
        a,b = self.a,self.b
        X=w2.view(w.shape[0],a,b)
        if self.epoch>-1 and self.epoch%args.rotation_update==0:
            for _ in range(3):
                #* update B
                V = self.R1.t()@X.detach()@self.R2
                B = torch.sign(V)
                #* update R1
                D1=sum([Bi@(self.R2.t())@(Xi.t()) for (Bi,Xi) in zip(B,X.detach())])
                if not args.use_gpu:
                    D1=D1.cpu()
                U1,S1,V1=torch.svd(D1)
                self.R1=(V1@(U1.t()))
                if not args.use_gpu:
                    self.R1=self.R1.to(X.device)
                #* update R2
                D2=sum([(Xi.t())@self.R1@Bi for (Xi,Bi) in zip(X.detach(),B)])
                if not args.use_gpu:
                    D2=D2.cpu()
                U2,S2,V2=torch.svd(D2)
                self.R2=(U2@(V2.t()))
                if not args.use_gpu:
                    self.R2=self.R2.to(X.device)
        self.Rweight=((self.R1.t())@X@(self.R2)).view_as(w)
        delta = self.Rweight.detach() - w2
        w3 = w2 + torch.abs(torch.sin(self.rotate)) * delta 

        self.Rotate = torch.mean(torch.abs(torch.sin(self.rotate)))
        #* binarize
        bw = BinaryQuantize().apply(w3, self.k.to(w.device), self.t.to(w.device))
        if args.a32:
            ba = a2
        else:
            ba = BinaryQuantize_a().apply(a2, self.k.to(w.device), self.t.to(w.device))
        #* 1bit conv
        output = F.conv2d(ba, bw, self.bias,
                          self.stride, self.padding,
                          self.dilation, self.groups)
        #* scaling factor
        output = output * self.alpha
        return output

        
class BinaryQuantize(Function):
    @staticmethod
    def forward(ctx, input, k, t):
        ctx.save_for_backward(input, k, t)
        out = torch.sign(input)
        return out

    @staticmethod
    def backward(ctx, grad_output):
        input, k, t = ctx.saved_tensors
        grad_input = k * (2*torch.sqrt(t**2/2) - torch.abs(t**2*input))
        grad_input = grad_input.clamp(min=0) * grad_output.clone()
        return grad_input, None, None

class BinaryQuantize_a(Function):
    @staticmethod
    def forward(ctx, input, k, t):
        ctx.save_for_backward(input, k, t)
        out = torch.sign(input)
        return out

    @staticmethod
    def backward(ctx, grad_output):
        input, k, t = ctx.saved_tensors
        k = torch.tensor(1.).to(input.device)
        t = max(t,torch.tensor(1.).to(input.device))
        grad_input = k * (2*torch.sqrt(t**2/2) - torch.abs(t**2*input))
        grad_input = grad_input.clamp(min=0) * grad_output.clone()
        return grad_input, None, None

def get_ab(N):
    sqrt = int(np.sqrt(N))
    for i in range(sqrt,0,-1):
        if N%i==0:
            return i,N//i 