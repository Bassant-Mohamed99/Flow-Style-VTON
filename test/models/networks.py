import torch
import torch.nn as nn
import torch.nn.parallel
import torch.nn.functional as F
#from torchvision import models
#from options.train_options import TrainOptions
import os

#opt = TrainOptions().parse()

class ResidualBlock(nn.Module):
    def __init__(self, in_features=64, norm_layer=nn.BatchNorm2d):
        super(ResidualBlock, self).__init__()
        self.relu = nn.ReLU(True)
        if norm_layer == None:
            self.block = nn.Sequential(
                nn.Conv2d(in_features, in_features, 3, 1, 1, bias=False),
                nn.ReLU(inplace=True),
                nn.Conv2d(in_features, in_features, 3, 1, 1, bias=False),
            )
        else:
            self.block = nn.Sequential(
                nn.Conv2d(in_features, in_features, 3, 1, 1, bias=False),
                norm_layer(in_features),
                nn.ReLU(inplace=True),
                nn.Conv2d(in_features, in_features, 3, 1, 1, bias=False),
                norm_layer(in_features)
            )

    def forward(self, x):
        residual = x
        out = self.block(x)
        out += residual
        out = self.relu(out)
        return out


class ResUnetGenerator(nn.Module):
    def __init__(self, input_nc, output_nc, num_downs, ngf=64,
                 norm_layer=nn.BatchNorm2d, use_dropout=False):
        super(ResUnetGenerator, self).__init__()
        # construct unet structure
        unet_block = ResUnetSkipConnectionBlock(ngf * 8, ngf * 8, input_nc=None, submodule=None, norm_layer=norm_layer, innermost=True)

        for i in range(num_downs - 5):
            unet_block = ResUnetSkipConnectionBlock(ngf * 8, ngf * 8, input_nc=None, submodule=unet_block, norm_layer=norm_layer, use_dropout=use_dropout)
        unet_block = ResUnetSkipConnectionBlock(ngf * 4, ngf * 8, input_nc=None, submodule=unet_block, norm_layer=norm_layer)
        unet_block = ResUnetSkipConnectionBlock(ngf * 2, ngf * 4, input_nc=None, submodule=unet_block, norm_layer=norm_layer)
        unet_block = ResUnetSkipConnectionBlock(ngf, ngf * 2, input_nc=None, submodule=unet_block, norm_layer=norm_layer)
        unet_block = ResUnetSkipConnectionBlock(output_nc, ngf, input_nc=input_nc, submodule=unet_block, outermost=True, norm_layer=norm_layer)

        self.model = unet_block

    def forward(self, input):
        return self.model(input)


# Defines the submodule with skip connection.
# X -------------------identity---------------------- X
#   |-- downsampling -- |submodule| -- upsampling --|
class SpatialAttention(nn.Module):
    def __init__(self, kernel_size=7):
        super(SpatialAttention, self).__init__()
        self.conv = nn.Conv2d(2, 1, kernel_size, padding=(kernel_size - 1) // 2, bias=False)

    def forward(self, x):
        avg_out = torch.mean(x, dim=1, keepdim=True)
        max_out, _ = torch.max(x, dim=1, keepdim=True)
        x = torch.cat([avg_out, max_out], dim=1)
        x = self.conv(x)
        return torch.sigmoid(x)

class ResUnetSkipConnectionBlock(nn.Module):
    def __init__(self, outer_nc, inner_nc, input_nc=None,
                 submodule=None, outermost=False, innermost=False, norm_layer=nn.InstanceNorm2d):
        super(ResUnetSkipConnectionBlock, self).__init__()
        self.outermost = outermost
        use_bias = norm_layer == nn.InstanceNorm2d

        if input_nc is None:
            input_nc = outer_nc

        downconv = nn.Conv2d(input_nc, inner_nc, kernel_size=3, stride=2, padding=1, bias=use_bias)
        upsample = nn.Upsample(scale_factor=2, mode='bilinear', align_corners=True)
        upconv = nn.Conv2d(inner_nc * 2, outer_nc, kernel_size=3, stride=1, padding=1, bias=use_bias)

        downrelu = nn.ReLU(inplace=True)
        uprelu = nn.ReLU(inplace=True)

        if norm_layer is not None:
            downnorm = norm_layer(inner_nc)
            upnorm = norm_layer(outer_nc)

        self.attention = SpatialAttention()  # Add Spatial Attention

        if outermost:
            down = [downconv, downrelu]
            up = [upsample, upconv]
            model = down + [submodule] + up
        elif innermost:
            down = [downconv, downrelu]
            up = [upsample, upconv, upnorm, uprelu]
            model = down + up
        else:
            down = [downconv, downnorm, downrelu]
            up = [upsample, upconv, upnorm, uprelu]
            model = down + [submodule] + up

        self.model = nn.Sequential(*model)

    def forward(self, x):
        if self.outermost:
             print(f"Input shape before conv: {x.shape}")  # Debugging
             print(f"Output shape after conv: {x.shape}")  # Debugging
             return self.model(x)
        else:
            skip_x = x
            x = self.model(x)
            attention_map = self.attention(skip_x)  # Apply spatial attention
            x = torch.cat([x, skip_x * attention_map], dim=1)  # Multiply attention map with skip connection
            return x



class Vgg19(nn.Module):
    def __init__(self, requires_grad=False):
        super(Vgg19, self).__init__()
        vgg_pretrained_features = models.vgg19(pretrained=True).features
        self.slice1 = nn.Sequential()
        self.slice2 = nn.Sequential()
        self.slice3 = nn.Sequential()
        self.slice4 = nn.Sequential()
        self.slice5 = nn.Sequential()
        for x in range(2):
            self.slice1.add_module(str(x), vgg_pretrained_features[x])
        for x in range(2, 7):
            self.slice2.add_module(str(x), vgg_pretrained_features[x])
        for x in range(7, 12):
            self.slice3.add_module(str(x), vgg_pretrained_features[x])
        for x in range(12, 21):
            self.slice4.add_module(str(x), vgg_pretrained_features[x])
        for x in range(21, 30):
            self.slice5.add_module(str(x), vgg_pretrained_features[x])
        if not requires_grad:
            for param in self.parameters():
                param.requires_grad = False

    def forward(self, X):
        h_relu1 = self.slice1(X)
        h_relu2 = self.slice2(h_relu1)
        h_relu3 = self.slice3(h_relu2)
        h_relu4 = self.slice4(h_relu3)
        h_relu5 = self.slice5(h_relu4)
        out = [h_relu1, h_relu2, h_relu3, h_relu4, h_relu5]
        return out

class VGGLoss(nn.Module):
    def __init__(self, layids = None):
        super(VGGLoss, self).__init__()
        self.vgg = Vgg19()
        self.vgg.cuda()
        self.criterion = nn.L1Loss()
        self.weights = [1.0/32, 1.0/16, 1.0/8, 1.0/4, 1.0]
        self.layids = layids

    def forward(self, x, y):
        x_vgg, y_vgg = self.vgg(x), self.vgg(y)
        loss = 0
        if self.layids is None:
            self.layids = list(range(len(x_vgg)))
        for i in self.layids:
            loss += self.weights[i] * self.criterion(x_vgg[i], y_vgg[i].detach())
        return loss

def save_checkpoint(model, save_path):
    if not os.path.exists(os.path.dirname(save_path)):
        os.makedirs(os.path.dirname(save_path))
    torch.save(model.state_dict(), save_path)


def load_checkpoint_parallel(model, checkpoint_path):
    if not os.path.exists(checkpoint_path):
        print('No checkpoint!')
        return

    checkpoint = torch.load(checkpoint_path, map_location='cuda:{}'.format(opt.local_rank))
    checkpoint_new = model.state_dict()

    for param in checkpoint_new:
        modified_param = param.replace('attention.conv', 'new_layer_name')  # Change this!
        if modified_param in checkpoint:
            checkpoint_new[param] = checkpoint[modified_param]

    model.load_state_dict(checkpoint_new, strict=False)


def load_checkpoint_part_parallel(model, checkpoint_path):
    if not os.path.exists(checkpoint_path):
        print('No checkpoint!')
        return

    checkpoint = torch.load(checkpoint_path, map_location='cuda:{}'.format(opt.local_rank))
    checkpoint_new = model.state_dict()

    for param in checkpoint_new:
        if 'cond_' not in param and 'aflow_net.netRefine' not in param or 'aflow_net.cond_style' in param:
            modified_param = param.replace('attention.conv', 'new_layer_name')  # Change this!
            if modified_param in checkpoint:
                checkpoint_new[param] = checkpoint[modified_param]

    model.load_state_dict(checkpoint_new, strict=False)


def load_checkpoint(model, checkpoint_path):
    if not os.path.exists(checkpoint_path):
        print('No checkpoint!')
        return

    checkpoint = torch.load(checkpoint_path)
    checkpoint_new = model.state_dict()

    for param in checkpoint_new:
        modified_param = param.replace('attention.conv', 'new_layer_name')  # Change this!
        if modified_param in checkpoint:
            checkpoint_new[param] = checkpoint[modified_param]

    model.load_state_dict(checkpoint_new, strict=False)



