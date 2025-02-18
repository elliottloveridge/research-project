from copy import deepcopy
from collections import OrderedDict
import logging
import csv
import numpy as np
from functools import partial
import os
import distiller
from distiller.scheduler import CompressionScheduler

from torch import nn
from torch import optim
from torch.optim import lr_scheduler

from opts import parse_opts
from model import generate_model
# NOTE: do I also need an import of utils?
from util import *
import test
from mean import get_mean, get_std
from spatial_transforms import *
from temporal_transforms import *
from target_transforms import ClassLabel, VideoID
from target_transforms import Compose as TargetCompose
from dataset import get_training_set, get_validation_set, get_test_set
from utils.model_pruning import Pruner

msglogger = logging.getLogger()


def perform_sensitivity_analysis(model, net_params, sparsities, test_func, group):
    """Perform a sensitivity test for a model's weights parameters.

    The model should be trained to maximum accuracy, because we aim to understand
    the behavior of the model's performance in relation to pruning of a specific
    weights tensor.

    By default this function will test all of the model's parameters.

    The return value is a complex sensitivities dictionary: the dictionary's
    key is the name (string) of the weights tensor.  The value is another dictionary,
    where the tested sparsity-level is the key, and a (top1, top5, loss) tuple
    is the value.
    Below is an example of such a dictionary:

    .. code-block:: python
    {'features.module.6.weight':    {0.0:  (56.518, 79.07,  1.9159),
                                     0.05: (56.492, 79.1,   1.9161),
                                     0.10: (56.212, 78.854, 1.9315),
                                     0.15: (35.424, 60.3,   3.0866)},
     'classifier.module.1.weight':  {0.0:  (56.518, 79.07,  1.9159),
                                     0.05: (56.514, 79.07,  1.9159),
                                     0.10: (56.434, 79.074, 1.9138),
                                     0.15: (54.454, 77.854, 2.3127)} }

    The test_func is expected to execute the model on a test/validation dataset,
    and return the results for top1 and top5 accuracies, and the loss value.
    """
    if group not in ['element', 'filter', 'channel']:
        raise ValueError("group parameter contains an illegal value: {}".format(group))
    sensitivities = OrderedDict()

    for param_name in net_params:
        if model.state_dict()[param_name].dim() not in [2, 4, 5]:
            continue

        # Make a copy of the model, because when we apply the zeros mask (i.e.
        # perform pruning), the model's weights are altered
        model_cpy = deepcopy(model)

        sensitivity = OrderedDict()
        for sparsity_level in sparsities:
            sparsity_level = float(sparsity_level)
            print("Testing sensitivity of %s [%0.1f%% sparsity]"%(param_name, sparsity_level*100))
            if group == 'element':
                # Element-wise sparasity
                sparsity_levels = {param_name: sparsity_level}
                pruner = distiller.pruning.SparsityLevelParameterPruner(name="sensitivity", levels=sparsity_levels)
            elif group == 'filter':
                # Filter ranking
                if model.state_dict()[param_name].dim() != 5:
                    continue
                pruner = distiller.pruning.L1RankedStructureParameterPruner("sensitivity",
                                                                            group_type="Filters",
                                                                            desired_sparsity=sparsity_level,
                                                                            weights=param_name)
            elif group == 'channel':
                # Filter ranking
                if model.state_dict()[param_name].dim() != 5:
                    continue
                pruner = distiller.pruning.L1RankedStructureParameterPruner("sensitivity",
                                                                            group_type="Channels",
                                                                            desired_sparsity=sparsity_level,
                                                                            weights=param_name)

            policy = distiller.PruningPolicy(pruner, pruner_args=None)
            scheduler = CompressionScheduler(model_cpy)
            scheduler.add_policy(policy, epochs=[0])

            # Compute the pruning mask per the pruner and apply the mask on the weights
            scheduler.on_epoch_begin(0)
            scheduler.mask_all_weights()

            # Test and record the performance of the pruned model
            prec1, prec5, loss = test_func(model=model_cpy)

            # prec1, prec5, loss = test.test_eval(test_loader, model_cpy, opt, test_data.class_names, criterion)
            sensitivity[sparsity_level] = (prec1, prec5, loss)
            sensitivities[param_name] = sensitivity
    return sensitivities


def sensitivities_to_png(sensitivities, fname):
    """Create a mulitplot of the sensitivities.

    The 'sensitivities' argument is expected to have the dict-of-dict structure
    described in the documentation of perform_sensitivity_test.
    """
    try:
        # sudo apt-get install python3-tk
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
    except ImportError:
        print("WARNING: Function plot_sensitivity requires package matplotlib which"
              "is not installed in your execution environment.\n"
              "Skipping the PNG file generation")
        return

    msglogger.info("Generating sensitivity graph")

    for param_name, sensitivity in sensitivities.items():
        sense = [values[1] for sparsity, values in sensitivity.items()]
        sparsities = [sparsity for sparsity, values in sensitivity.items()]
        plt.plot(sparsities, sense, label=param_name[7:])

    plt.ylabel('top5')
    plt.xlabel('sparsity')
    plt.title('Pruning Sensitivity')
    # FIXME: need to get this to work with all weights, change the name of params for this?
    plt.legend(loc='lower left', fontsize='xx-small',
               ncol=2, mode="expand", borderaxespad=0.)
               # bbox_to_anchor=(1.05, 1) # NOTE: use this to put legend outside of figure
    plt.savefig(fname, format='png')


def sensitivities_to_csv(sensitivities, fname):
    """Create a CSV file listing from the sensitivities dictionary.

    The 'sensitivities' argument is expected to have the dict-of-dict structure
    described in the documentation of perform_sensitivity_test.
    """
    with open(fname, 'w') as csv_file:
        writer = csv.writer(csv_file)
        # write the header
        writer.writerow(['parameter', 'sparsity', 'top1', 'top5', 'loss'])
        for param_name, sensitivity in sensitivities.items():
            for sparsity, values in sensitivity.items():
                writer.writerow([param_name] + [sparsity] + list(values))


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

torch.manual_seed(opt.manual_seed)

model, parameters = generate_model(opt)

criterion = nn.CrossEntropyLoss()
if not opt.no_cuda:
    criterion = criterion.cuda()

if opt.no_mean_norm and not opt.std_norm:
    norm_method = Normalize([0, 0, 0], [1, 1, 1])
elif not opt.std_norm:
    norm_method = Normalize(opt.mean, [1, 1, 1])
else:
    norm_method = Normalize(opt.mean, opt.std)



best_prec1 = 0


if opt.resume_path:
    print('loading checkpoint {}'.format(opt.resume_path))
    checkpoint = torch.load(opt.resume_path)
    model.to(torch.device("cuda"))
    # #%%%% need to refine the below code
    #
    # # NOTE: create new OrderedDict with additional `module.`
    # new_state_dict = OrderedDict()
    # for k, v in checkpoint['state_dict'].items():
    #     # NOTE: this is hacky, remove it and get working without
    #     name = 'module.' + k
    #     new_state_dict[name] = v
    #
    # # %%%% end of refine code

    assert opt.arch == checkpoint['arch']
    best_prec1 = checkpoint['best_prec1']
    opt.begin_epoch = checkpoint['epoch']

    # model.load_state_dict(new_state_dict)

    model.load_state_dict(checkpoint['state_dict'])

# get parameters from file
params = Pruner.get_names(opt)

# introduce a range of sparsity values
sparse_rng = [0.0, 0.2, 0.4, 0.6, 0.8, 1.0]

# same as test call in main.py but includes sampling
spatial_transform = Compose([
    Scale(int(opt.sample_size / opt.scale_in_test)),
    CornerCrop(opt.sample_size, opt.crop_position_in_test),
    ToTensor(opt.norm_value), norm_method])
temporal_transform = TemporalRandomCrop(opt.sample_duration, opt.downsample)
target_transform = ClassLabel()

test_data = get_test_set(opt, spatial_transform, temporal_transform,
                         target_transform)


subset_ind = np.random.randint(0, len(test_data), size=(1, 400))
test_subset = torch.utils.data.Subset(test_data, subset_ind[0])
test_loader = torch.utils.data.DataLoader(
    test_subset,
    batch_size=16,
    shuffle=False,
    num_workers=opt.n_threads,
    pin_memory=True)

test_func = partial(test.test_eval, data_loader=test_loader, criterion=criterion, opt=opt)

sense = perform_sensitivity_analysis(model, params, sparsities=sparse_rng,
test_func=test_func, group='element')

f = opt.arch
if opt.arch in ['resnet', 'csn']:
    f += str(opt.model_depth)
f_png = f + '.png'
f_csv = f + '.csv'

sensitivities_to_png(sense, os.path.join(opt.result_path, f_png))
sensitivities_to_csv(sense, os.path.join(opt.result_path, f_csv))

# FIXME: doesn't yet add the best sensitivity choice and input into model summary
