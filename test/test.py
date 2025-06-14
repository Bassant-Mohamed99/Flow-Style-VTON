import time
from options.test_options import TestOptions
from data.data_loader_test import CreateDataLoader
from models.networks import ResUnetGenerator, load_checkpoint
from models.afwm import AFWM
from skimage.exposure import match_histograms
import torch.nn as nn
import os
import numpy as np
import torch
import cv2
import torch.nn.functional as F
from torchvision import utils
from util import flow_util




opt = TestOptions().parse()


def de_offset(s_grid):
    [b,_,h,w] = s_grid.size()


    x = torch.arange(w).view(1, -1).expand(h, -1).float()
    y = torch.arange(h).view(-1, 1).expand(-1, w).float()
    x = 2*x/(w-1)-1
    y = 2*y/(h-1)-1
    grid = torch.stack([x,y], dim=0).float().cuda()
    grid = grid.unsqueeze(0).expand(b, -1, -1, -1)

    offset = grid - s_grid

    offset_x = offset[:,0,:,:] * (w-1) / 2
    offset_y = offset[:,1,:,:] * (h-1) / 2

    offset = torch.cat((offset_y,offset_x),0)
    
    return  offset
    
def match_color(src_tensor, ref_tensor):
    # Inputs: Bx3xHxW tensors
    src = src_tensor[0].permute(1, 2, 0).detach().cpu().numpy()
    ref = ref_tensor[0].permute(1, 2, 0).detach().cpu().numpy()
    
    matched = match_histograms(src, ref, channel_axis=-1)
    matched = torch.from_numpy(matched).permute(2, 0, 1).unsqueeze(0).float().to(src_tensor.device)
    return matched

def tta_forward(generator, warped_cloth, edge, real_image):
    outputs = []

    inp = torch.cat([real_image, warped_cloth, edge], dim=1)
    outputs.append(generator(inp))

    # Horizontal flip
    wc_flip = torch.flip(warped_cloth, dims=[3])
    ri_flip = torch.flip(real_image, dims=[3])
    edge_flip = torch.flip(edge, dims=[3])
    inp_flip = torch.cat([ri_flip, wc_flip, edge_flip], dim=1)
    out_flip = generator(inp_flip)
    out_flip = torch.flip(out_flip, dims=[3])

    return torch.mean(torch.stack(outputs + [out_flip]), dim=0)

def edge_aware_filter(tensor_img):
    img = tensor_img.squeeze(0).permute(1, 2, 0).detach().cpu().numpy()
    img = np.clip(img * 255, 0, 255).astype(np.uint8)
    filtered = cv2.edgePreservingFilter(img, flags=1, sigma_s=60, sigma_r=0.4)
    filtered = filtered.astype(np.float32) / 255.0
    return torch.from_numpy(filtered).permute(2, 0, 1).unsqueeze(0).to(tensor_img.device)



start_epoch, epoch_iter = 1, 0

f2c = flow_util.flow2color()

data_loader = CreateDataLoader(opt)
dataset = data_loader.load_data()
dataset_size = len(data_loader)
print(dataset_size)
#import ipdb; ipdb.set_trace()
warp_model = AFWM(opt, 3)
print(warp_model)
warp_model.eval()
warp_model.cuda()
load_checkpoint(warp_model, opt.warp_checkpoint)

gen_model = ResUnetGenerator(7, 4, 5, ngf=64, norm_layer=nn.BatchNorm2d)
#print(gen_model)
gen_model.eval()
gen_model.cuda()
load_checkpoint(gen_model, opt.gen_checkpoint)

total_steps = (start_epoch-1) * dataset_size + epoch_iter
step = 0
step_per_batch = dataset_size / opt.batchSize

if not os.path.exists('our_t_results'):
  os.mkdir('our_t_results')

for epoch in range(1,2):

    for i, data in enumerate(dataset, start=epoch_iter):
        iter_start_time = time.time()
        total_steps += opt.batchSize
        epoch_iter += opt.batchSize

        real_image = data['image']
        clothes = data['clothes']
        ##edge is extracted from the clothes image with the built-in function in python
        edge = data['edge']
        edge = torch.FloatTensor((edge.detach().numpy() > 0.5).astype(np.int64))
        clothes = clothes * edge        

        #import ipdb; ipdb.set_trace()

        flow_out = warp_model(real_image.cuda(), clothes.cuda())
        warped_cloth, last_flow, = flow_out
        # ✅ Color correction step (histogram matching)
        warped_cloth = match_color(warped_cloth, clothes.cuda())
        warped_edge = F.grid_sample(edge.cuda(), last_flow.permute(0, 2, 3, 1),
                          mode='bilinear', padding_mode='zeros')

        gen_outputs = tta_forward(gen_model, warped_cloth, warped_edge, real_image.cuda())
        p_rendered, m_composite = torch.split(gen_outputs, [3, 1], 1)
        p_rendered = torch.tanh(p_rendered)
        m_composite = torch.sigmoid(m_composite)
        m_composite = m_composite * warped_edge
        p_tryon = warped_cloth * m_composite + p_rendered * (1 - m_composite)
        p_tryon = edge_aware_filter(p_tryon)

        path = 'results/' + opt.name
        os.makedirs(path, exist_ok=True)
        #sub_path = path + '/PFAFN'
        #os.makedirs(sub_path,exist_ok=True)
        print(data['p_name'])

        if step % 1 == 0:
            
            ## save try-on image only
            # Apply edge-aware smoothing before saving (optional)
            p_tryon = edge_aware_filter(p_tryon)            
            utils.save_image(
                p_tryon,
                os.path.join('./our_t_results', data['p_name'][0]),
                nrow=int(1),
                normalize=True,
                value_range=(-1,1),
            )
            
            ## save person image, garment, flow, warped garment, and try-on image
            
            #a = real_image.float().cuda()
            #b = clothes.cuda()
            #flow_offset = de_offset(last_flow)
            #flow_color = f2c(flow_offset).cuda()
            #c= warped_cloth.cuda()
            #d = p_tryon
            #combine = torch.cat([a[0],b[0], flow_color, c[0], d[0]], 2).squeeze()
            #utils.save_image(
            #    combine,
            #    os.path.join('./im_gar_flow_wg', data['p_name'][0]),
            #    nrow=int(1),
            #    normalize=True,
            #    range=(-1,1),
            #)
            

        step += 1
        if epoch_iter >= dataset_size:
            break


