import os
import sys
import json
import numpy as np
import torch
import pandas as pd

import distiller
from distiller.data_loggers import collect_quant_stats

from torch import nn
from torch import optim
from torch.optim import lr_scheduler

from opts import parse_opts
from model import generate_model
from util import *
import test
from mean import get_mean, get_std
from spatial_transforms import *
from temporal_transforms import *
from target_transforms import ClassLabel, VideoID
from target_transforms import Compose as TargetCompose
from dataset import get_training_set, get_validation_set, get_test_set

from calculate_FLOP import model_info

# opts kept the same even if not needed
opt = parse_opts()
if opt.root_path != '':
    opt.video_path = os.path.join(opt.root_path, opt.video_path)
    opt.annotation_path = os.path.join(opt.root_path, opt.annotation_path)
    opt.result_path = os.path.join(opt.root_path, opt.result_path)
    if not os.path.exists(opt.result_path):
        os.makedirs(opt.result_path)
    if opt.resume_path:
        opt.resume_path = os.path.join(opt.root_path, opt.resume_path)
opt.arch = '{}'.format(opt.model)
opt.mean = get_mean(opt.norm_value, dataset=opt.mean_dataset)
opt.std = get_std(opt.norm_value)
# NOTE: removed opt.store_name arg from here

# NOTE: added for norm_method used in test - need to check what it does
if opt.no_mean_norm and not opt.std_norm:
    norm_method = Normalize([0, 0, 0], [1, 1, 1])
elif not opt.std_norm:
    norm_method = Normalize(opt.mean, [1, 1, 1])
else:
    norm_method = Normalize(opt.mean, opt.std)

torch.manual_seed(opt.manual_seed)

model, parameters = generate_model(opt)

best_prec1 = 0
if opt.resume_path:
    print('loading checkpoint {}'.format(opt.resume_path))
    checkpoint = torch.load(opt.resume_path)
    assert opt.arch == checkpoint['arch']
    best_prec1 = checkpoint['best_prec1']
    opt.begin_epoch = checkpoint['epoch']
    model.load_state_dict(checkpoint['state_dict'])

distiller.utils.assign_layer_fq_names(model)
collector = QuantCalibrationStatsCollector(model)
stats_file = os.path.join(opt.result_path, 'quantization_stats.yaml')

if not os.path.isfile(stats_file):
    def eval_for_stats(model):
        evaluate(model, val_data)
    collect_quant_stats(model, eval_for_stats, save_dir=stas_file)