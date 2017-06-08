import random
import pprint
import sys
import time
import numpy as np
from optparse import OptionParser
import pickle

from keras import backend as K
from keras.optimizers import Adam
from keras.layers import Input
from keras.models import Model
from keras_frcnn import config, data_generators
from keras_frcnn import losses as losses
from keras_frcnn import resnet as nn
import keras_frcnn.roi_helpers as roi_helpers

sys.setrecursionlimit(40000)

parser = OptionParser()

parser.add_option("-p", "--path", dest="train_path", help="Path to training data.")
parser.add_option("-o", "--parser", dest="parser", help="Parser to use. One of simple or pascal_voc",
                  default="pascal_voc"),
parser.add_option("-n", "--num_rois", dest="num_rois",
                  help="Number of ROIs per iteration. Higher means more memory use.", default=32)
parser.add_option("--hf", dest="horizontal_flips", help="Augment with horizontal flips in training. (Default=true).",
                  action="store_true", default=False)
parser.add_option("--vf", dest="vertical_flips", help="Augment with vertical flips in training. (Default=false).",
                  action="store_true", default=False)
parser.add_option("--rot", "--rot_90", dest="rot_90",
                  help="Augment with 90 degree rotations in training. (Default=false).",
                  action="store_true", default=False)
parser.add_option("--num_epochs", dest="num_epochs", help="Number of epochs.", default=2000)
parser.add_option("--config_filename", dest="config_filename", help=
"Location to store all the metadata related to the training (to be used when testing).",
                  default="config.pickle")
parser.add_option("--output_weight_path", dest="output_weight_path", help="Output path for weights.",
                  default='./model_frcnn.hdf5')
parser.add_option("--input_weight_path", dest="input_weight_path",
                  help="Input path for weights. If not specified, will try to load default weights provided by keras.")

(options, args) = parser.parse_args()

if not options.train_path:  # if filename is not given
    parser.error('Error: path to training data must be specified. Pass --path to command line')

if options.parser == 'pascal_voc':
    from keras_frcnn.pascal_voc_parser import get_data
elif options.parser == 'simple':
    from keras_frcnn.simple_parser import get_data
else:
    raise ValueError("Command line option parser must be one of 'pascal_voc' or 'simple'")

# pass the settings from the command line, and persist them in the config object
C = config.Config()

C.num_rois = int(options.num_rois)
C.use_horizontal_flips = bool(options.horizontal_flips)
C.use_vertical_flips = bool(options.vertical_flips)
C.rot_90 = bool(options.rot_90)

C.model_path = options.output_weight_path

if options.input_weight_path:
    C.base_net_weights = options.input_weight_path

all_imgs, classes_count, class_mapping = get_data(options.train_path)

if 'bg' not in classes_count:
    classes_count['bg'] = 0
    class_mapping['bg'] = len(class_mapping)

C.class_mapping = class_mapping

inv_map = {v: k for k, v in class_mapping.iteritems()}

print('Training images per class:')
pprint.pprint(classes_count)
print('Num classes (including bg) = {}'.format(len(classes_count)))

config_output_filename = options.config_filename

with open(config_output_filename, 'w') as config_f:
    pickle.dump(C, config_f)
    print('Config has been written to {}, and can be loaded when testing to ensure correct results'.format(
        config_output_filename))

random.shuffle(all_imgs)

num_imgs = len(all_imgs)

train_imgs = [s for s in all_imgs if s['imageset'] == 'trainval']
val_imgs = [s for s in all_imgs if s['imageset'] == 'test']

print('Num train samples {}'.format(len(train_imgs)))
print('Num val samples {}'.format(len(val_imgs)))

data_gen_train = data_generators.get_anchor_gt(train_imgs, classes_count, C, K.image_dim_ordering(), mode='train')
data_gen_val = data_generators.get_anchor_gt(val_imgs, classes_count, C, K.image_dim_ordering(), mode='val')

if K.image_dim_ordering() == 'th':
    input_shape_img = (3, None, None)
else:
    input_shape_img = (None, None, 3)

img_input = Input(shape=input_shape_img)
roi_input = Input(shape=(C.num_rois, 4))

# define the base network (resnet here, can be VGG, Inception, etc)
shared_layers = nn.nn_base(img_input, trainable=True)

# define the RPN, built on the base layers
num_anchors = len(C.anchor_box_scales) * len(C.anchor_box_ratios)
rpn = nn.rpn(shared_layers, num_anchors)

classifier = nn.classifier(shared_layers, roi_input, C.num_rois, nb_classes=len(classes_count), trainable=True)

model_rpn = Model(img_input, rpn[:2])
model_classifier = Model([img_input, roi_input], classifier)

# this is a model that holds both the RPN and the classifier, used to load/save weights for the models
model_all = Model([img_input, roi_input], rpn[:2] + classifier)

try:
    print('loading weights from {}'.format(C.base_net_weights))
    model_rpn.load_weights(C.base_net_weights, by_name=True)
    model_classifier.load_weights(C.base_net_weights, by_name=True)
except:
    print('Could not load pretrained model weights. Weights can be found at {} and {}'.format(
        'https://github.com/fchollet/deep-learning-models/releases/download/v0.2/resnet50_weights_th_dim_ordering_th_kernels_notop.h5',
        'https://github.com/fchollet/deep-learning-models/releases/download/v0.2/resnet50_weights_tf_dim_ordering_tf_kernels_notop.h5'
    ))

optimizer = Adam(lr=1e-5)
optimizer_classifier = Adam(lr=1e-5)
model_rpn.compile(optimizer=optimizer, loss=[losses.rpn_loss_cls(num_anchors), losses.rpn_loss_regr(num_anchors)])
model_classifier.compile(optimizer=optimizer_classifier,
                         loss=[losses.class_loss_cls, losses.class_loss_regr(len(classes_count) - 1)],
                         metrics={'dense_class_{}'.format(len(classes_count)): 'accuracy'})
model_all.compile(optimizer='sgd', loss='mae')

epoch_length = 100
num_epochs = int(options.num_epochs)
iter_num = 0
epoch_num = 0

losses = np.zeros((epoch_length, 5))
losses_val = np.zeros((epoch_length, 5))

rpn_accuracy_rpn_monitor = []
rpn_accuracy_rpn_monitor_val = []

rpn_accuracy_for_epoch = []
rpn_accuracy_for_epoch_val = []

start_time = time.time()

best_loss = np.Inf
best_loss_val = np.Inf

class_mapping_inv = {v: k for k, v in class_mapping.iteritems()}
print('Starting training')

while True:
    try:

        if len(rpn_accuracy_rpn_monitor) == epoch_length and C.verbose:
            mean_overlapping_bboxes = float(sum(rpn_accuracy_rpn_monitor)) / len(rpn_accuracy_rpn_monitor)
            rpn_accuracy_rpn_monitor = []
            print('Average number of overlapping bounding boxes from RPN = {} for {} previous iterations'.format(
                mean_overlapping_bboxes, epoch_length))
            if mean_overlapping_bboxes == 0:
                print(
                    'RPN is not producing bounding boxes that overlap the ground truth boxes. Results will not be satisfactory. Keep training.')

        print("Start get training data")

        X, Y, img_data = data_gen_train.next()

        X_val, Y_val, img_data_val = data_gen_val.next()

        print("Finish get training data")

        loss_rpn = model_rpn.train_on_batch(X, Y)

        loss_rpn_val = model_rpn.test_on_batch(X_val, Y_val)

        P_rpn = model_rpn.predict_on_batch(X)

        P_rpn_val = model_rpn.predict_on_batch(X_val)

        R = roi_helpers.rpn_to_roi(P_rpn[0], P_rpn[1], C, K.image_dim_ordering(), use_regr=True, overlap_thresh=0.7,
                                   max_boxes=300)

        R_val = roi_helpers.rpn_to_roi(P_rpn_val[0], P_rpn_val[1], C, K.image_dim_ordering(), use_regr=True,
                                       overlap_thresh=0.7,
                                       max_boxes=300)

        # note: calc_iou converts from (x1,y1,x2,y2) to (x,y,w,h) format
        X2, Y1, Y2 = roi_helpers.calc_iou(R, img_data, C, class_mapping)

        X2_val, Y1_val, Y2_val = roi_helpers.calc_iou(R_val, img_data_val, C, class_mapping)

        if X2 is None:
            rpn_accuracy_rpn_monitor.append(0)
            rpn_accuracy_for_epoch.append(0)
            continue

        if X2_val is None:
            rpn_accuracy_rpn_monitor_val.append(0)
            rpn_accuracy_for_epoch_val.append(0)
            continue

        neg_samples = np.where(Y1[0, :, -1] == 1)
        pos_samples = np.where(Y1[0, :, -1] == 0)

        neg_samples_val = np.where(Y1_val[0, :, -1] == 1)
        pos_samples_val = np.where(Y1_val[0, :, -1] == 0)

        if len(neg_samples) > 0:
            neg_samples = neg_samples[0]
        else:
            neg_samples = []

        if len(neg_samples_val) > 0:
            neg_samples_val = neg_samples_val[0]
        else:
            neg_samples_val = []

        if len(pos_samples_val) > 0:
            pos_samples_val = pos_samples_val[0]
        else:
            pos_samples_val = []

        rpn_accuracy_rpn_monitor.append(len(pos_samples))
        rpn_accuracy_for_epoch.append((len(pos_samples)))

        rpn_accuracy_rpn_monitor_val.append(len(pos_samples_val))
        rpn_accuracy_for_epoch_val.append((len(pos_samples_val)))

        if C.num_rois > 1:
            if len(pos_samples) < C.num_rois / 2:
                selected_pos_samples = pos_samples.tolist()
            else:
                selected_pos_samples = np.random.choice(pos_samples, C.num_rois / 2, replace=False).tolist()
            try:
                selected_neg_samples = np.random.choice(neg_samples, C.num_rois - len(selected_pos_samples),
                                                        replace=False).tolist()
            except:
                selected_neg_samples = np.random.choice(neg_samples, C.num_rois - len(selected_pos_samples),
                                                        replace=True).tolist()

            sel_samples = selected_pos_samples + selected_neg_samples
        else:
            # in the extreme case where num_rois = 1, we pick a random pos or neg sample
            selected_pos_samples = pos_samples.tolist()
            selected_neg_samples = neg_samples.tolist()
            if np.random.randint(0, 2):
                sel_samples = random.choice(neg_samples)
            else:
                sel_samples = random.choice(pos_samples)

        if C.num_rois > 1:
            if len(pos_samples_val) < C.num_rois / 2:
                selected_pos_samples_val = pos_samples_val.tolist()
            else:
                selected_pos_samples_val = np.random.choice(pos_samples_val, C.num_rois / 2, replace=False).tolist()
            try:
                selected_neg_samples_val = np.random.choice(neg_samples_val, C.num_rois - len(selected_pos_samples_val),
                                                            replace=False).tolist()
            except:
                selected_neg_samples_val = np.random.choice(neg_samples_val, C.num_rois - len(selected_pos_samples_val),
                                                            replace=True).tolist()

            sel_samples_val = selected_pos_samples_val + selected_neg_samples_val
        else:
            # in the extreme case where num_rois = 1, we pick a random pos or neg sample
            selected_pos_samples_val = pos_samples_val.tolist()
            selected_neg_samples_val = neg_samples_val.tolist()
            if np.random.randint(0, 2):
                sel_samples_val = random.choice(neg_samples_val)
            else:
                sel_samples_val = random.choice(pos_samples_val)

        loss_class = model_classifier.train_on_batch([X, X2[:, sel_samples, :]],
                                                     [Y1[:, sel_samples, :], Y2[:, sel_samples, :]])

        loss_class_val = model_classifier.test_on_batch([X_val, X2_val[:, sel_samples_val, :]],
                                                        [Y1_val[:, sel_samples_val, :], Y2_val[:, sel_samples_val, :]])

        losses[iter_num, 0] = loss_rpn[1]
        losses[iter_num, 1] = loss_rpn[2]

        losses[iter_num, 2] = loss_class[1]
        losses[iter_num, 3] = loss_class[2]
        losses[iter_num, 4] = loss_class[3]

        losses_val[iter_num, 0] = loss_rpn_val[1]
        losses_val[iter_num, 1] = loss_rpn_val[2]

        losses_val[iter_num, 2] = loss_class_val[1]
        losses_val[iter_num, 3] = loss_class_val[2]
        losses_val[iter_num, 4] = loss_class_val[3]

        iter_num += 1

        if iter_num == epoch_length:
            loss_rpn_cls = np.mean(losses[:, 0])
            loss_rpn_regr = np.mean(losses[:, 1])
            loss_class_cls = np.mean(losses[:, 2])
            loss_class_regr = np.mean(losses[:, 3])
            class_acc = np.mean(losses[:, 4])

            mean_overlapping_bboxes = float(sum(rpn_accuracy_for_epoch)) / len(rpn_accuracy_for_epoch)
            rpn_accuracy_for_epoch = []

            if C.verbose:
                print('Epoch {}:'.format(epoch_num))
                print('Mean number of bounding boxes from RPN overlapping ground truth boxes: {}'.format(
                    mean_overlapping_bboxes))
                print('Classifier accuracy for bounding boxes from RPN: {}'.format(class_acc))
                print('Loss RPN classifier: {}'.format((loss_rpn_cls)))
                print('Loss RPN regression: {}'.format((loss_rpn_regr)))
                print('Loss Classifier classifier: {}'.format((loss_class_cls)))
                print('Loss Classifier regression: {}'.format((loss_class_regr)))
                # print('Elapsed time: {}'.format(time.time() - start_time))
            else:
                print(
                    'loss_rpn_cls,{},loss_rpn_regr,{},loss_class_cls,{},loss_class_regr,{},class_acc,{},elapsed_time,{}'.format(
                        loss_rpn_cls, loss_rpn_regr, loss_class_cls, loss_class_regr, class_acc,
                        time.time() - start_time))

            loss_rpn_cls_val = np.mean(losses_val[:, 0])
            loss_rpn_regr_val = np.mean(losses_val[:, 1])
            loss_class_cls_val = np.mean(losses_val[:, 2])
            loss_class_regr_val = np.mean(losses_val[:, 3])
            class_acc_val = np.mean(losses_val[:, 4])

            mean_overlapping_bboxes_val = float(sum(rpn_accuracy_for_epoch_val)) / len(rpn_accuracy_for_epoch_val)
            rpn_accuracy_for_epoch_val = []

            if C.verbose:

                print('Val mean number of bounding boxes from RPN overlapping ground truth boxes: {}'.format(
                    mean_overlapping_bboxes_val))
                print('Val classifier accuracy for bounding boxes from RPN: {}'.format(class_acc_val))
                print('Val loss RPN classifier: {}'.format((loss_rpn_cls_val)))
                print('Val loss RPN regression: {}'.format((loss_rpn_regr_val)))
                print('Val loss Classifier classifier: {}'.format((loss_class_cls_val)))
                print('Val oss Classifier regression: {}'.format((loss_class_regr_val)))
            else:
                print(
                    'Val loss_rpn_cls,{},loss_rpn_regr,{},loss_class_cls,{},loss_class_regr,{},class_acc,{},elapsed_time,{}'.format(
                        loss_rpn_cls_val, loss_rpn_regr_val, loss_class_cls_val, loss_class_regr_val, class_acc_val,
                        time.time() - start_time))

            curr_loss = loss_rpn_cls + loss_rpn_regr + loss_class_cls + loss_class_regr
            curr_loss_val = loss_rpn_cls_val + loss_rpn_regr_val + loss_class_cls_val + loss_class_regr_val
            # iter_num = 0
            # start_time = time.time()
            epoch_num += 1
            if epoch_num == 1 or curr_loss < best_loss:
                if C.verbose:
                    print('Total loss decreased from {} to {}, saving weights'.format(best_loss, curr_loss))
                best_loss = curr_loss
                model_all.save_weights(C.model_path)

                if C.verbose:
                    print('Val total loss decreased from {} to {}, saving weights'.format(best_loss_val, curr_loss_val))
                best_loss_val = curr_loss_val

            iter_num = 0
            start_time = time.time()
        if epoch_num == num_epochs:
            print('Training complete, exiting.')
            sys.exit()
    except Exception as e:
        print('Exception: {}'.format(e))
        continue
