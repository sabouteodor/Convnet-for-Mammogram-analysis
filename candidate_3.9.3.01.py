import numpy as np
import os
import wget
from sklearn.model_selection import train_test_split
import tensorflow as tf
from training_utils import download_file, get_batches, load_validation_data, \
    download_data, get_training_data, load_weights, flatten, _conv2d_batch_norm, _read_images, \
    read_and_decode_single_example, augment
import argparse
from tensorboard import summary as summary_lib

# If number of epochs has been passed in use that, otherwise default to 50
parser = argparse.ArgumentParser()
parser.add_argument("-e", "--epochs", help="number of epochs to train", default=30, type=int)
parser.add_argument("-d", "--data", help="which dataset to use", default=12, type=int)
parser.add_argument("-m", "--model", help="model to initialize weights with", default=None)
parser.add_argument("-r", "--restore", help="model to restore and continue training", default=None)
parser.add_argument("-l", "--label", help="how to classify data", default="mask")
parser.add_argument("-a", "--action", help="action to perform", default="train")
parser.add_argument("-f", "--freeze", help="whether to freeze convolutional layers", nargs='?', const=True,
                    default=False)
parser.add_argument("-s", "--stop", help="stop gradient at pool5", nargs='?', const=True, default=False)
parser.add_argument("-t", "--threshold", help="decision threshold", default=0.5, type=float)
parser.add_argument("-c", "--contrast", help="contrast adjustment, if any", default=None, type=float)
parser.add_argument("-n", "--normalize", help="apply per image normalization", nargs='?', const=True, default=False)
parser.add_argument("-w", "--weight", help="weight to give to positive examples in cross-entropy", default=15,
                    type=float)
parser.add_argument("-v", "--version", help="version or run number to assign to model name", default="")
parser.add_argument("--distort", help="use online data augmentation", default=False, const=True, nargs="?")
parser.add_argument("--size", help="size of image to crop (default 640)", default=480, type=int)
args = parser.parse_args()

epochs = args.epochs
dataset = args.data
init_model = args.model
restore_model = args.restore
how = args.label
action = args.action
threshold = args.threshold
freeze = args.freeze
stop = args.stop
contrast = args.contrast
normalize = args.normalize
size = args.size
weight = args.weight - 1
distort = args.distort
version = args.version

# figure out how to label the model name
if how == "label":
    model_label = "l"
elif how == "normal":
    model_label = "b"
elif how == "mask":
    model_label = "m"
else:
    model_label = "x"

# precalculated pixel mean of images
mu = 104.1353

# download the data
# download_data(what=dataset)

## config
batch_size = 16

if dataset != 100:
    train_files, total_records = get_training_data(what=dataset)
else:
    # use each image 3 times for each epoch since we are taking random crops
    total_records = len(os.listdir(os.path.join("data", "train_images"))) * 3

## Hyperparameters
epsilon = 1e-8

# learning rate
epochs_per_decay = 10
decay_factor = 0.85
staircase = True

# if we are retraining some layers start with smaller learning rate
if not stop and not freeze:
    starting_rate = 0.001
else:
    starting_rate = 0.0001

# start with a slightly lower learning rate since we are re-training later layers
# starting_rate = 0.0007

# learning rate decay variables
steps_per_epoch = int(total_records / batch_size)
print("Steps per epoch:", steps_per_epoch)

# lambdas
lamC = 0.00001
lamF = 0.00250

# use dropout
dropout = True
fcdropout_rate = 0.25
convdropout_rate = 0.01
pooldropout_rate = 0.1
upsample_dropout = 0.05

if how == "label":
    num_classes = 5
elif how == "normal":
    num_classes = 2
elif how == "mass":
    num_classes = 3
elif how == "benign":
    num_classes = 3
elif how == "mask":
    num_classes = 2

print("Number of classes:", num_classes)
print("Image crop size:", size)

## Build the graph
graph = tf.Graph()

model_name = "model_s3.9.3.01" + model_label + "." + str(dataset) + str(version)
## Change Log
# 0.0.0.4 - increase pool3 to 3x3 with stride 3
# 0.0.0.6 - reduce pool 3 stride back to 2
# 0.0.0.7 - reduce lambda for l2 reg
# 0.0.0.8 - increase conv1 to 7x7 stride 2
# 0.0.0.9 - disable per image normalization
# 0.0.0.10 - commented out batch norm in conv layers, added conv4 and changed stride of convs to 1, increased FC lambda
# 0.0.0.11 - turn dropout for conv layers on
# 0.0.0.12 - added batch norm after pooling layers, increase pool dropout, decrease conv dropout, added extra conv layer to reduce data dimensionality
# 0.0.0.13 - added precision and f1 summaries
# 0.0.0.14 - fixing batch normalization, I don't think it's going to work after each pool
# 0.0.0.15 - reduced xentropy weighting term
# 0.0.0.17 - replaced initial 5x5 conv layers with 3 3x3 layers
# 0.0.0.18 - changed stride of first conv to 2 from 1
# 0.0.0.19 - doubled units in two fc layers
# 0.0.0.20 - lowered learning rate, put a batch norm back in
# 0.0.0.21 - put all batch norms back in
# 0.0.0.22 - increased lambdaC, removed dropout from conv layers
# 1.0.0.23 - added extra conv layers
# 1.0.0.27 - updates to training code and metrics
# 1.0.0.28 - using weighted x-entropy to improve recall
# 1.0.0.29 - updated code to work training to classify for multiple classes
# 1.0.0.29f - putting weighted x-entropy back
# 1.0.0.30b - changed some hyperparameters
# 1.0.0.31l - added decision threshold to predictions
# 1.0.0.33 - scaling input data
# 1.0.0.34 - centering data by 127, not by mean
# 1.0.0.35 - not centering data, just scaling it
# 2.0.0.35 - turning into fcn
# 2.0.0.36 - scaling and centering data?
# 3.0.0.36 - adjusting to do segmentation instead of classification
# 3.0.0.37 - trying to get this to train faster
# 3.0.0.38 - adding tiny value to logits to avoid xe of NaN
# 3.0.0.39 - doing metrics per pixel instead of per image
# 3.0.0.40 - adjusted graph so we can do online data augmentation and labels will be transformed in same way as images
# 3.1.0.40 - adding some layers back in that were previously removed to take more advantage of pre-trained model
# 3.1.0.41 - changed skip connections to try to make it a bit more stable
# 3.1.0.42 - changed one more skip connection
# 3.1.0.43 - trying to not restore batch norm to see if that helps with NaN at test time
# 3.1.0.44 - increased size of upconv filters to try to reduce patchiness of result, removed fc layer 3 as it was losing a lot of data
# 3.1.0.45 - adding some dropout to try to regularize
# 3.2.0.45 - restructuring to accept 320x320 images as input
# 3.2.0.46 - increased sizes of upsample filters
# 3.2.0.47 - changed number of filters again to speed up training
# 3.2.1.48 - adding extra skip connection to try to get better predictions
# 3.2.1.49 - renamed one upconv layer so they can be isolated and trained
# 3.2.2.01 - tweaking the upsampling layers
# 3.2.3.01 - going to train from scratch so adding some extras layers and such
# 3.2.4.01 - switching from tf records to reading entire images and taking random crops for more training data
# 3.2.4.02 - fixed bug where one layer was missing activation function
# 3.2.5.01 - rearranging some skip connections to use conv layers rather than pools
# 3.2.5.02 - adding more bottlenecks and batch norms
# 3.2.5.03 - replaced another skip pool connection with a conv + reduce channels, fixed reduce layers from transpose to normal convs, added regularization to transpose conv layers
# 3.2.6.01 - replacing convs in upsample section with transpose convs with stride 1
# 3.2.7.01 - changing upsampling to try to improve quality
# 3.2.8.01 - remove skip connection so results don't resemble image so much
# 3.3.1.01 - adding some more convolutional layers to upsampling section
# 3.3.2.01 - changing FC1 from 5x5 filter to 2x2 and changing unpool1 from 5x5 to 2x2 so we can more easily alter input image size
# 3.3.3.01 - changing structure of upsampling section
# 3.5.0.01 - again redoing the upsampling section to simplify
# 3.5.0.01 - making path to images a placeholder so we can evaluate the test images the same way as train images
# 3.6.0.01 - simplifying upsampling section to retrain from scratch
# 3.6.0.02 - removing some layers to speed up training
# 3.6.0.03 - removing dilated convolutions, they seem to be messing things up
# 3.6.0.04 - putting dilated convolution back, replacing first transpose conv with resize
# 3.6.0.05 - changing number of filters in upsampling section
# 3.6.1.05 - scaling input to half original size instead of 2/3, maybe will speed up training and/or improve results?
# 3.6.4.01 - adding extra dilated convolution and super simplifying upsampling section
# 3.7.0.01 - some minor tweaks to graph, then retraining from scratch
# 3.8.0.01 - increasing size of model, it may be too small?
# 3.9.0.01 - and now for something completely different (redoing upsampling section again - logits is now 320x320, resized to 640x640)
# 3.9.1.01 - changing structure a bit
# 3.9.2.01 - changing last layer to sigmoid instead of logits
# 3.9.2.02 - adding some residual connections
# 3.9.3.01 - downsizing image more initially to add more layers, removed pool4, used dilated convolutions afterwards

with graph.as_default():
    training = tf.placeholder(dtype=tf.bool, name="is_training")
    is_testing = tf.placeholder(dtype=bool, shape=(), name="is_testing")

    # create global step for decaying learning rate
    global_step = tf.Variable(0, trainable=False)

    learning_rate = tf.train.exponential_decay(starting_rate,
                                               global_step,
                                               steps_per_epoch * epochs_per_decay,
                                               decay_factor,
                                               staircase=staircase)

    with tf.name_scope('inputs') as scope:
        with tf.device('/cpu:0'):
            if dataset == 100:
                # decode the image
                image, label = _read_images("./data/train_images/", size, scale_by=0.66, distort=False,
                                            standardize=normalize)
            else:
                image, label = read_and_decode_single_example(train_files, label_type=how, normalize=False,
                                                              distort=False, size=640)

            X_def, y_def = tf.train.shuffle_batch([image, label], batch_size=batch_size, capacity=75 * batch_size,
                                                  seed=None, num_threads=6, min_after_dequeue=30 * batch_size)

        # Placeholders
        X = tf.placeholder_with_default(X_def, shape=[None, size, size, 1])
        y = tf.placeholder_with_default(y_def, shape=[None, size, size, 1])

        X_adj = tf.cast(X, tf.float32)
        y_adj = tf.cast(y, tf.int32)

        # optional online data augmentation
        if distort:
            X_adj, y_adj = augment(X_adj, y_adj, horizontal_flip=True, augment_labels=True, vertical_flip=True, mixup=0)

    # Convolutional layer 1 - 320x320x32
    with tf.name_scope('conv0.1') as scope:
        conv1 = tf.layers.conv2d(
            X_adj,
            filters=32,
            kernel_size=(3, 3),
            strides=(2, 2),
            padding='SAME',
            activation=None,
            kernel_initializer=tf.truncated_normal_initializer(stddev=5e-2, seed=100),
            kernel_regularizer=tf.contrib.layers.l2_regularizer(scale=lamC),
            name='conv0.1'
        )

        conv1 = tf.layers.batch_normalization(
            conv1,
            axis=-1,
            momentum=0.99,
            epsilon=epsilon,
            center=True,
            scale=True,
            beta_initializer=tf.zeros_initializer(),
            gamma_initializer=tf.ones_initializer(),
            moving_mean_initializer=tf.zeros_initializer(),
            moving_variance_initializer=tf.ones_initializer(),
            training=training,
            fused=True,
            name='bn0.1'
        )

        # apply relu
        conv1_bn_relu = tf.nn.relu(conv1, name='relu0.1')

    # 320x320x32
    with tf.name_scope('conv0.2') as scope:
        conv1 = tf.layers.conv2d(
            conv1_bn_relu,
            filters=32,
            kernel_size=(3, 3),
            strides=(2, 2),
            padding='SAME',
            activation=None,
            kernel_initializer=tf.truncated_normal_initializer(stddev=5e-2, seed=100),
            kernel_regularizer=tf.contrib.layers.l2_regularizer(scale=lamC),
            name='conv0.2'
        )

        conv1 = tf.layers.batch_normalization(
            conv1,
            axis=-1,
            momentum=0.99,
            epsilon=epsilon,
            center=True,
            scale=True,
            beta_initializer=tf.zeros_initializer(),
            gamma_initializer=tf.ones_initializer(),
            moving_mean_initializer=tf.zeros_initializer(),
            moving_variance_initializer=tf.ones_initializer(),
            training=training,
            fused=True,
            name='bn0.2'
        )

        # apply relu
        conv1_bn_relu = tf.nn.relu(conv1, name='relu0.2')

    # 160x160x48
    with tf.name_scope('conv1.0') as scope:
        conv1 = tf.layers.conv2d(
            conv1_bn_relu,
            filters=48,
            kernel_size=(3, 3),
            strides=(2, 2),
            padding='SAME',
            activation=None,
            kernel_initializer=tf.truncated_normal_initializer(stddev=5e-2, seed=100),
            kernel_regularizer=tf.contrib.layers.l2_regularizer(scale=lamC),
            name='conv1.0'
        )

        conv1 = tf.layers.batch_normalization(
            conv1,
            axis=-1,
            momentum=0.99,
            epsilon=epsilon,
            center=True,
            scale=True,
            beta_initializer=tf.zeros_initializer(),
            gamma_initializer=tf.ones_initializer(),
            moving_mean_initializer=tf.zeros_initializer(),
            moving_variance_initializer=tf.ones_initializer(),
            training=training,
            fused=True,
            name='bn1.0'
        )

        # apply relu
        conv1_bn_relu = tf.nn.relu(conv1, name='relu1.0')

    # 160x160x48
    with tf.name_scope('conv1.1') as scope:
        conv11 = tf.layers.conv2d(
            conv1_bn_relu,
            filters=48,
            kernel_size=(3, 3),
            strides=(1, 1),
            padding='SAME',
            activation=None,
            kernel_initializer=tf.truncated_normal_initializer(stddev=5e-2, seed=101),
            kernel_regularizer=tf.contrib.layers.l2_regularizer(scale=lamC),
            name='conv1.1'
        )

        conv11 = tf.layers.batch_normalization(
            conv11,
            axis=-1,
            momentum=0.99,
            epsilon=epsilon,
            center=True,
            scale=True,
            beta_initializer=tf.zeros_initializer(),
            gamma_initializer=tf.ones_initializer(),
            moving_mean_initializer=tf.zeros_initializer(),
            moving_variance_initializer=tf.ones_initializer(),
            training=training,
            fused=True,
            name='bn1.1'
        )

        # apply relu
        conv11 = tf.nn.relu(conv11, name='relu1.1')

    # 160x160x48
    with tf.name_scope('conv1.2') as scope:
        conv12 = tf.layers.conv2d(
            conv11,
            filters=48,
            kernel_size=(3, 3),
            strides=(1, 1),
            padding='SAME',
            activation=None,
            kernel_initializer=tf.truncated_normal_initializer(stddev=5e-2, seed=1101),
            kernel_regularizer=tf.contrib.layers.l2_regularizer(scale=lamC),
            name='conv1.2'
        )

        conv12 = tf.layers.batch_normalization(
            conv12,
            axis=-1,
            momentum=0.99,
            epsilon=epsilon,
            center=True,
            scale=True,
            beta_initializer=tf.zeros_initializer(),
            gamma_initializer=tf.ones_initializer(),
            moving_mean_initializer=tf.zeros_initializer(),
            moving_variance_initializer=tf.ones_initializer(),
            training=training,
            fused=True,
            name='bn1.2'
        )

        # apply relu
        conv12_relu = tf.nn.relu(conv12, name='relu1.1')

    # 80x80x48
    with tf.name_scope('pool1') as scope:
        pool1 = tf.layers.max_pooling2d(
            conv12_relu,
            pool_size=(3, 3),
            strides=(2, 2),
            padding='SAME',
            name='pool1'
        )

        # optional dropout
        if dropout:
            pool1 = tf.layers.dropout(pool1, rate=pooldropout_rate, seed=103, training=training)

    # Convolutional layer 2 - 80x80x64
    with tf.name_scope('conv2.1') as scope:
        conv2 = tf.layers.conv2d(
            pool1,
            filters=64,
            kernel_size=(3, 3),
            strides=(1, 1),
            padding='SAME',
            activation=None,
            kernel_initializer=tf.truncated_normal_initializer(stddev=5e-2, seed=104),
            kernel_regularizer=tf.contrib.layers.l2_regularizer(scale=lamC),
            name='conv2.1'
        )

        conv2 = tf.layers.batch_normalization(
            conv2,
            axis=-1,
            momentum=0.99,
            epsilon=epsilon,
            center=True,
            scale=True,
            beta_initializer=tf.zeros_initializer(),
            gamma_initializer=tf.ones_initializer(),
            moving_mean_initializer=tf.zeros_initializer(),
            moving_variance_initializer=tf.ones_initializer(),
            training=training,
            fused=True,
            name='bn2.1'
        )

        # apply relu
        conv2_relu = tf.nn.relu(conv2, name='relu2.1')

    # Convolutional layer 2.2 - 80x80x64
    with tf.name_scope('conv2.2') as scope:
        conv22 = tf.layers.conv2d(
            conv2_relu,
            filters=64,
            kernel_size=(3, 3),
            strides=(1, 1),
            padding='SAME',
            activation=None,
            kernel_initializer=tf.truncated_normal_initializer(stddev=5e-2, seed=1104),
            kernel_regularizer=tf.contrib.layers.l2_regularizer(scale=lamC),
            name='conv2.2'
        )

        conv22 = tf.layers.batch_normalization(
            conv22,
            axis=-1,
            momentum=0.99,
            epsilon=epsilon,
            center=True,
            scale=True,
            beta_initializer=tf.zeros_initializer(),
            gamma_initializer=tf.ones_initializer(),
            moving_mean_initializer=tf.zeros_initializer(),
            moving_variance_initializer=tf.ones_initializer(),
            training=training,
            fused=True,
            name='bn2.2'
        )

        # apply relu
        conv22 = tf.nn.relu(conv22, name='relu2.2')

    # Convolutional layer 2.3 - 80x80x64
    with tf.name_scope('conv2.3') as scope:
        conv22 = tf.layers.conv2d(
            conv22,
            filters=64,
            kernel_size=(3, 3),
            strides=(1, 1),
            padding='SAME',
            activation=None,
            kernel_initializer=tf.truncated_normal_initializer(stddev=5e-2, seed=1104),
            kernel_regularizer=tf.contrib.layers.l2_regularizer(scale=lamC),
            name='conv2.3'
        )

        conv22 = tf.layers.batch_normalization(
            conv22,
            axis=-1,
            momentum=0.99,
            epsilon=epsilon,
            center=True,
            scale=True,
            beta_initializer=tf.zeros_initializer(),
            gamma_initializer=tf.ones_initializer(),
            moving_mean_initializer=tf.zeros_initializer(),
            moving_variance_initializer=tf.ones_initializer(),
            training=training,
            fused=True,
            name='bn2.3'
        )

        # skip connection
        conv22 = conv22 + conv2

        # apply relu
        conv22_relu = tf.nn.relu(conv22, name='relu2.3')

    # Max pooling layer 2 - 40x40x64
    with tf.name_scope('pool2') as scope:
        pool2 = tf.layers.max_pooling2d(
            conv22_relu,
            pool_size=(2, 2),
            strides=(2, 2),
            padding='SAME',
            name='pool2'
        )

        # optional dropout
        if dropout:
            pool2 = tf.layers.dropout(pool2, rate=pooldropout_rate, seed=106, training=training)

    # Convolutional layer 3 - 40x40x128
    with tf.name_scope('conv3.1') as scope:
        conv3 = tf.layers.conv2d(
            pool2,
            filters=128,
            kernel_size=(3, 3),
            strides=(1, 1),
            padding='SAME',
            activation=None,
            kernel_initializer=tf.truncated_normal_initializer(stddev=5e-2, seed=107),
            kernel_regularizer=tf.contrib.layers.l2_regularizer(scale=lamC),
            name='conv3.1'
        )

        conv3 = tf.layers.batch_normalization(
            conv3,
            axis=-1,
            momentum=0.99,
            epsilon=epsilon,
            center=True,
            scale=True,
            beta_initializer=tf.zeros_initializer(),
            gamma_initializer=tf.ones_initializer(),
            moving_mean_initializer=tf.zeros_initializer(),
            moving_variance_initializer=tf.ones_initializer(),
            training=training,
            fused=True,
            name='bn3.1'
        )

        # apply relu
        conv3_relu = tf.nn.relu(conv3, name='relu3.1')

    # Convolutional layer 3.2 - 40x40x128
    with tf.name_scope('conv3.2') as scope:
        conv32 = tf.layers.conv2d(
            conv3_relu,
            filters=128,
            kernel_size=(3, 3),
            strides=(1, 1),
            padding='SAME',
            activation=None,
            kernel_initializer=tf.truncated_normal_initializer(stddev=5e-2, seed=1107),
            kernel_regularizer=tf.contrib.layers.l2_regularizer(scale=lamC),
            name='conv3.2'
        )

        conv32 = tf.layers.batch_normalization(
            conv32,
            axis=-1,
            momentum=0.99,
            epsilon=epsilon,
            center=True,
            scale=True,
            beta_initializer=tf.zeros_initializer(),
            gamma_initializer=tf.ones_initializer(),
            moving_mean_initializer=tf.zeros_initializer(),
            moving_variance_initializer=tf.ones_initializer(),
            training=training,
            fused=True,
            name='bn3.2'
        )

        # apply relu
        conv32_relu = tf.nn.relu(conv32, name='relu3.2')

    with tf.name_scope('conv3.3') as scope:
        conv32 = tf.layers.conv2d(
            conv32_relu,
            filters=128,
            kernel_size=(3, 3),
            strides=(1, 1),
            padding='SAME',
            activation=None,
            kernel_initializer=tf.truncated_normal_initializer(stddev=5e-2, seed=1107),
            kernel_regularizer=tf.contrib.layers.l2_regularizer(scale=lamC),
            name='conv3.3'
        )

        conv32 = tf.layers.batch_normalization(
            conv32,
            axis=-1,
            momentum=0.99,
            epsilon=epsilon,
            center=True,
            scale=True,
            beta_initializer=tf.zeros_initializer(),
            gamma_initializer=tf.ones_initializer(),
            moving_mean_initializer=tf.zeros_initializer(),
            moving_variance_initializer=tf.ones_initializer(),
            training=training,
            fused=True,
            name='bn3.3'
        )

        conv32 = conv32 + conv3

        # apply relu
        conv32_relu = tf.nn.relu(conv32, name='relu3.2')

    # Max pooling layer 3 - 20x20x128
    with tf.name_scope('pool3') as scope:
        pool3 = tf.layers.max_pooling2d(
            conv32_relu,
            pool_size=(2, 2),
            strides=(2, 2),
            padding='SAME',
            name='pool3'
        )

        if dropout:
            pool3 = tf.layers.dropout(pool3, rate=pooldropout_rate, seed=109, training=training)

    # Convolutional layer 4 - 20x20x256
    with tf.name_scope('conv4') as scope:
        conv4 = tf.layers.conv2d(
            pool3,
            filters=256,
            kernel_size=(3, 3),
            strides=(1, 1),
            padding='SAME',
            activation=None,
            kernel_initializer=tf.truncated_normal_initializer(stddev=5e-2, seed=110),
            kernel_regularizer=tf.contrib.layers.l2_regularizer(scale=lamC),
            name='conv4'
        )

        conv4 = tf.layers.batch_normalization(
            conv4,
            axis=-1,
            momentum=0.99,
            epsilon=epsilon,
            center=True,
            scale=True,
            beta_initializer=tf.zeros_initializer(),
            gamma_initializer=tf.ones_initializer(),
            moving_mean_initializer=tf.zeros_initializer(),
            moving_variance_initializer=tf.ones_initializer(),
            training=training,
            fused=True,
            name='bn4'
        )

        # apply relu
        conv4_bn_relu = tf.nn.relu(conv4, name='relu4')

    # 20x20x256
    with tf.name_scope('conv4.1') as scope:
        conv41 = tf.layers.conv2d(
            conv4_bn_relu,
            filters=256,
            kernel_size=(3, 3),
            strides=(1, 1),
            padding='SAME',
            activation=None,
            kernel_initializer=tf.truncated_normal_initializer(stddev=5e-2, seed=1710),
            kernel_regularizer=tf.contrib.layers.l2_regularizer(scale=lamC),
            name='conv4.1'
        )

        conv41 = tf.layers.batch_normalization(
            conv41,
            axis=-1,
            momentum=0.99,
            epsilon=epsilon,
            center=True,
            scale=True,
            beta_initializer=tf.zeros_initializer(),
            gamma_initializer=tf.ones_initializer(),
            moving_mean_initializer=tf.zeros_initializer(),
            moving_variance_initializer=tf.ones_initializer(),
            training=training,
            fused=True,
            name='bn4.1'
        )

        # apply relu
        conv41_bn_relu = tf.nn.relu(conv41, name='relu4.1')

    # 20x20x256
    with tf.name_scope('conv4.2') as scope:
        conv41 = tf.layers.conv2d(
            conv41_bn_relu,
            filters=256,
            kernel_size=(3, 3),
            strides=(1, 1),
            padding='SAME',
            activation=None,
            kernel_initializer=tf.truncated_normal_initializer(stddev=5e-2, seed=1710),
            kernel_regularizer=tf.contrib.layers.l2_regularizer(scale=lamC),
            name='conv4.2'
        )

        conv41 = tf.layers.batch_normalization(
            conv41,
            axis=-1,
            momentum=0.99,
            epsilon=epsilon,
            center=True,
            scale=True,
            beta_initializer=tf.zeros_initializer(),
            gamma_initializer=tf.ones_initializer(),
            moving_mean_initializer=tf.zeros_initializer(),
            moving_variance_initializer=tf.ones_initializer(),
            training=training,
            fused=True,
            name='bn4.2'
        )

        # apply relu
        conv41 = tf.nn.relu(conv41, name='relu4.2')

    # 20x20x256
    with tf.name_scope('conv4.3') as scope:
        conv41 = tf.layers.conv2d(
            conv41,
            filters=256,
            kernel_size=(3, 3),
            strides=(1, 1),
            padding='SAME',
            activation=None,
            kernel_initializer=tf.truncated_normal_initializer(stddev=5e-2, seed=1710),
            kernel_regularizer=tf.contrib.layers.l2_regularizer(scale=lamC),
            name='conv4.3'
        )

        conv41 = tf.layers.batch_normalization(
            conv41,
            axis=-1,
            momentum=0.99,
            epsilon=epsilon,
            center=True,
            scale=True,
            beta_initializer=tf.zeros_initializer(),
            gamma_initializer=tf.ones_initializer(),
            moving_mean_initializer=tf.zeros_initializer(),
            moving_variance_initializer=tf.ones_initializer(),
            training=training,
            fused=True,
            name='bn4.3'
        )

        # residual connection
        conv41 = conv41 + conv4

        # apply relu
        conv41 = tf.nn.relu(conv41, name='relu4.3')

    # Max pooling layer 4 - 20x20x256
    with tf.name_scope('pool4') as scope:
        pool4 = conv41
        # pool4 = tf.layers.max_pooling2d(
        #     conv41,
        #     pool_size=(2, 2),
        #     strides=(2, 2),
        #     padding='SAME',
        #     name='pool4'
        # )
        #
        # if dropout:
        #     pool4 = tf.layers.dropout(pool4, rate=pooldropout_rate, seed=112, training=training)

    # Convolutional layer 5 - 20x20x384 - dilated by 2
    with tf.name_scope('conv5') as scope:
        conv5 = tf.layers.conv2d(
            pool4,
            filters=512,
            kernel_size=(3, 3),
            strides=(1, 1),
            dilation_rate=(2, 2),
            padding='SAME',
            activation=None,
            kernel_initializer=tf.truncated_normal_initializer(stddev=5e-2, seed=113),
            kernel_regularizer=tf.contrib.layers.l2_regularizer(scale=lamC),
            name='conv5'
        )

        conv5 = tf.layers.batch_normalization(
            conv5,
            axis=-1,
            momentum=0.99,
            epsilon=epsilon,
            center=True,
            scale=True,
            beta_initializer=tf.zeros_initializer(),
            gamma_initializer=tf.ones_initializer(),
            moving_mean_initializer=tf.zeros_initializer(),
            moving_variance_initializer=tf.ones_initializer(),
            training=training,
            fused=True,
            name='bn5'
        )

        # apply relu
        conv5_bn_relu = tf.nn.relu(conv5, name='relu5')

    # 20x20x384
    with tf.name_scope('conv5.1') as scope:
        conv51 = tf.layers.conv2d(
            conv5_bn_relu,
            filters=512,
            kernel_size=(3, 3),
            strides=(1, 1),
            dilation_rate=(2, 2),
            padding='SAME',
            activation=None,
            kernel_initializer=tf.truncated_normal_initializer(stddev=5e-2, seed=11930),
            kernel_regularizer=tf.contrib.layers.l2_regularizer(scale=lamC),
            name='conv5.1'
        )

        conv51 = tf.layers.batch_normalization(
            conv51,
            axis=-1,
            momentum=0.99,
            epsilon=epsilon,
            center=True,
            scale=True,
            beta_initializer=tf.zeros_initializer(),
            gamma_initializer=tf.ones_initializer(),
            moving_mean_initializer=tf.zeros_initializer(),
            moving_variance_initializer=tf.ones_initializer(),
            training=training,
            fused=True,
            name='bn5.1'
        )

        # apply relu
        conv5_relu = tf.nn.relu(conv5, name='relu5.1')

    # 20x20x384
    with tf.name_scope('conv5.2') as scope:
        conv51 = tf.layers.conv2d(
            conv5_relu,
            filters=512,
            kernel_size=(3, 3),
            strides=(1, 1),
            dilation_rate=(2, 2),
            padding='SAME',
            activation=None,
            kernel_initializer=tf.truncated_normal_initializer(stddev=5e-2, seed=11930),
            kernel_regularizer=tf.contrib.layers.l2_regularizer(scale=lamC),
            name='conv5.2'
        )

        conv51 = tf.layers.batch_normalization(
            conv51,
            axis=-1,
            momentum=0.99,
            epsilon=epsilon,
            center=True,
            scale=True,
            beta_initializer=tf.zeros_initializer(),
            gamma_initializer=tf.ones_initializer(),
            moving_mean_initializer=tf.zeros_initializer(),
            moving_variance_initializer=tf.ones_initializer(),
            training=training,
            fused=True,
            name='bn5.2'
        )

        # apply relu
        conv51_relu = tf.nn.relu(conv51, name='relu5.2')

    # convolution w/ dilation 2 - 20x20x512
    with tf.name_scope('conv5.3') as scope:
        conv51 = tf.layers.conv2d(
            conv51_relu,
            filters=512,
            kernel_size=(3, 3),
            strides=(1, 1),
            padding='SAME',
            dilation_rate=(2, 2),
            activation=None,
            kernel_initializer=tf.truncated_normal_initializer(stddev=5e-2, seed=11931),
            kernel_regularizer=tf.contrib.layers.l2_regularizer(scale=lamC),
            name='conv5.3'
        )

        conv51 = tf.layers.batch_normalization(
            conv51,
            axis=-1,
            momentum=0.99,
            epsilon=epsilon,
            center=True,
            scale=True,
            beta_initializer=tf.zeros_initializer(),
            gamma_initializer=tf.ones_initializer(),
            moving_mean_initializer=tf.zeros_initializer(),
            moving_variance_initializer=tf.ones_initializer(),
            training=training,
            fused=True,
            name='bn5.3'
        )

        # skip connection
        conv51 = conv51 + conv5

        # apply relu
        conv51_relu = tf.nn.relu(conv51, name='relu5.2')

    # # convolution w/ dilation 2 - 20x20x512
    # with tf.name_scope('conv5.3') as scope:
    #     conv53 = tf.layers.conv2d(
    #         conv51_relu,
    #         filters=512,
    #         kernel_size=(3, 3),
    #         strides=(1, 1),
    #         padding='SAME',
    #         dilation_rate=(2, 2),
    #         activation=None,
    #         kernel_initializer=tf.truncated_normal_initializer(stddev=5e-2, seed=11932),
    #         kernel_regularizer=tf.contrib.layers.l2_regularizer(scale=lamC),
    #         name='conv5.3'
    #     )
    #
    #     conv53 = tf.layers.batch_normalization(
    #         conv53,
    #         axis=-1,
    #         momentum=0.99,
    #         epsilon=epsilon,
    #         center=True,
    #         scale=True,
    #         beta_initializer=tf.zeros_initializer(),
    #         gamma_initializer=tf.ones_initializer(),
    #         moving_mean_initializer=tf.zeros_initializer(),
    #         moving_variance_initializer=tf.ones_initializer(),
    #         training=training,
    #         fused=True,
    #         name='bn5.3'
    #     )
    #
    #     # apply relu
    #     conv53 = tf.nn.relu(conv53, name='relu5.3')

    # "fully connected" layer - 20x20x512
    with tf.name_scope('fc_1') as scope:
        fc1 = tf.layers.conv2d(
            conv51,
            filters=768,
            kernel_size=(1, 1),
            strides=(1, 1),
            padding='SAME',
            activation=None,
            kernel_initializer=tf.truncated_normal_initializer(stddev=5e-2, seed=11932),
            kernel_regularizer=tf.contrib.layers.l2_regularizer(scale=lamC),
            name='fc_1'
        )

        fc1 = tf.layers.batch_normalization(
            fc1,
            axis=-1,
            momentum=0.99,
            epsilon=epsilon,
            center=True,
            scale=True,
            beta_initializer=tf.zeros_initializer(),
            gamma_initializer=tf.ones_initializer(),
            moving_mean_initializer=tf.zeros_initializer(),
            moving_variance_initializer=tf.ones_initializer(),
            training=training,
            fused=True,
            name='bn_fc_1'
        )

        # apply relu
        fc1 = tf.nn.relu(fc1, name='relu_fc_1')

    # resize images - 80x80x768
    with tf.name_scope('resize_1') as scope:
        new_size = int(size // 8)
        unpool1 = tf.image.resize_images(fc1, size=[new_size, new_size],
                                         method=tf.image.ResizeMethod.NEAREST_NEIGHBOR)

    # 80x80x512
    with tf.name_scope('up_conv2') as scope:
        unpool21 = tf.layers.conv2d(
            unpool1,
            filters=512,
            kernel_size=(3, 3),
            strides=(1, 1),
            padding='SAME',
            activation=None,
            kernel_initializer=tf.truncated_normal_initializer(stddev=5e-2, seed=121435),
            kernel_regularizer=tf.contrib.layers.l2_regularizer(scale=lamC),
            name='up_conv2'
        )

        unpool21 = tf.layers.batch_normalization(
            unpool21,
            axis=-1,
            momentum=0.99,
            epsilon=epsilon,
            center=True,
            scale=True,
            beta_initializer=tf.zeros_initializer(),
            gamma_initializer=tf.ones_initializer(),
            moving_mean_initializer=tf.zeros_initializer(),
            moving_variance_initializer=tf.ones_initializer(),
            training=training,
            fused=True,
            name='bn_up_conv2'
        )

        # activation
        unpool21 = tf.nn.relu(unpool21, name="up_conv2_relu")

    # convolve resized image - 80x80x256
    with tf.name_scope('up_conv4') as scope:
        unpool4 = tf.layers.conv2d(
            unpool21,
            filters=256,
            kernel_size=(3, 3),
            strides=(1, 1),
            padding='SAME',
            activation=None,
            kernel_initializer=tf.truncated_normal_initializer(stddev=5e-2, seed=11756),
            kernel_regularizer=tf.contrib.layers.l2_regularizer(scale=lamC),
            name='up_conv4'
        )

        unpool4 = tf.layers.batch_normalization(
            unpool4,
            axis=-1,
            momentum=0.99,
            epsilon=epsilon,
            center=True,
            scale=True,
            beta_initializer=tf.zeros_initializer(),
            gamma_initializer=tf.ones_initializer(),
            moving_mean_initializer=tf.zeros_initializer(),
            moving_variance_initializer=tf.ones_initializer(),
            training=training,
            fused=True,
            name='bn_up_conv4'
        )

    # resize to 160x160x256
    with tf.name_scope('resize_6') as scope:
        unpool6 = tf.image.resize_images(unpool4, size=[size // 4, size // 4],
                                         method=tf.image.ResizeMethod.NEAREST_NEIGHBOR)

    # 160x160x128
    uconv5 = _conv2d_batch_norm(unpool6, 128, kernel_size=(3, 3), stride=(1, 1), training=training, lambd=lamC,
                                name="up_conv6", activation="relu")

    # 160x160x128
    uconv6 = _conv2d_batch_norm(uconv5, 128, kernel_size=(3, 3), stride=(1, 1), training=training, lambd=lamC,
                                name="up_conv7", activation="relu")

    # upsample - 320x320x64
    with tf.name_scope('upsample_4') as scope:
        up_conv7 = tf.layers.conv2d_transpose(
            uconv6,
            filters=64,
            kernel_size=(4, 4),
            strides=(2, 2),
            padding='SAME',
            activation=tf.nn.relu,
            kernel_initializer=tf.truncated_normal_initializer(stddev=5e-2, seed=117931),
            kernel_regularizer=None,
            name='upsample_4'
        )

    # 320x320x32 - transpose conv to smooth out artifacts
    with tf.name_scope('upsample_5') as scope:
        up_conv8 = tf.layers.conv2d_transpose(
            up_conv7,
            filters=32,
            kernel_size=(4, 4),
            strides=(1, 1),
            padding='SAME',
            activation=tf.nn.relu,
            kernel_initializer=tf.truncated_normal_initializer(stddev=5e-2, seed=117932),
            kernel_regularizer=None,
            name='upsample_5'
        )

    # last conv layer - 320x320x32
    uconv9 = _conv2d_batch_norm(up_conv8, 32, kernel_size=(3, 3), stride=(1, 1), training=training, lambd=lamC,
                                name="up_conv9", activation="relu")

    # logits - 32x320x2
    with tf.name_scope('logits') as scope:
        logits = tf.layers.conv2d(
            uconv9,
            filters=1,
            kernel_size=(3, 3),
            strides=(1, 1),
            padding='SAME',
            activation=None,
            kernel_initializer=tf.truncated_normal_initializer(stddev=5e-2, seed=117933),
            kernel_regularizer=None,
            name='logits'
        )

    # resize the logits
    with tf.name_scope('resize_11') as scope:
        logits = tf.image.resize_images(logits, size=[size, size],
                                         method=tf.image.ResizeMethod.NEAREST_NEIGHBOR)

    # softmax the logits and take the last dimension
    logits_sm = tf.sigmoid(logits)

    with tf.variable_scope('conv0.1', reuse=True):
        conv_kernels1 = tf.get_variable('kernel')
        kernel_transposed = tf.transpose(conv_kernels1, [3, 0, 1, 2])

    with tf.variable_scope('visualization'):
        tf.summary.image('conv0.1/filters', kernel_transposed, max_outputs=32, collections=["kernels"])

    # This will weight the positive examples higher so as to improve recall
    weights = tf.multiply(tf.cast(weight, tf.float32), tf.cast(tf.greater(y_adj, 0), tf.float32)) + 1

    mean_ce = tf.reduce_mean(
        tf.losses.sigmoid_cross_entropy(multi_class_labels=y_adj, logits=logits_sm, weights=weights))

    # Add in l2 loss
    loss = mean_ce + tf.losses.get_regularization_loss()

    # Adam optimizer
    optimizer = tf.train.AdamOptimizer(learning_rate=learning_rate)

    # Minimize cross-entropy - freeze certain layers depending on input
    if freeze:
        # make some collections so we can specify what to train
        deconv_all = tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES, "up_conv")
        fc_vars = tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES, "fc")
        bottleneck_vars = tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES, "bottleneck")
        logits_vars = tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES, "logits")
        upsample_vars = tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES, "upsample")
        conv_vars_5 = tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES, "conv5")

        # create a training step for vars that should be trained
        # train_op_2 = optimizer.minimize(loss, global_step=global_step, var_list=up_conv5_vars)
        train_op_2 = optimizer.minimize(loss, global_step=global_step,
                                        var_list=bottleneck_vars + logits_vars + deconv_all + fc_vars + upsample_vars + conv_vars_5)

    train_op_1 = optimizer.minimize(loss, global_step=global_step)

    # if we reshape the predictions it won't work with images of other sizes
    predictions = tf.round(logits_sm)

    # squash the predictions into a per image prediction - negative images will have a max of 0
    pred_sum = tf.reduce_sum(predictions, axis=[1, 2])
    image_predictions = tf.cast(tf.greater(pred_sum, (size * size // 750)), dtype=tf.uint8)
    image_truth = tf.reduce_max(y_adj, axis=[1, 2])

    # set a threshold on the predictions so we ignore images with only a few positive pixels
    pred_sum = tf.reduce_sum(predictions, axis=[1, 2])
    image_predictions = tf.cast(tf.greater(pred_sum, (size * size // 750)), dtype=tf.uint8)

    # get the accuracy per pixel
    accuracy, acc_op = tf.metrics.accuracy(
        labels=y_adj,
        predictions=predictions,
        updates_collections=[tf.GraphKeys.UPDATE_OPS, 'metrics_ops'],
        name="accuracy",
    )
    # calculate recall and precision per pixel
    recall, rec_op = tf.metrics.recall(labels=y_adj, predictions=predictions,
                                       updates_collections=[tf.GraphKeys.UPDATE_OPS, 'metrics_ops'],
                                       name="pixel_recall")

    precision, prec_op = tf.metrics.precision(labels=y_adj, predictions=predictions,
                                              updates_collections=[tf.GraphKeys.UPDATE_OPS, 'metrics_ops'],
                                              name="pixel_precision")

    f1_score = 2 * ((precision * recall) / (precision + recall))

    # per image metrics
    image_accuracy, image_acc_op = tf.metrics.accuracy(
        labels=image_truth,
        predictions=image_predictions,
        updates_collections=[tf.GraphKeys.UPDATE_OPS, 'metrics_ops'],
        name="image_accuracy",
    )

    image_recall, image_rec_op = tf.metrics.recall(labels=image_truth, predictions=image_predictions,
                                                   name="image_recall",
                                                   updates_collections=['extra_metrics_ops'])
    image_precision, image_prec_op = tf.metrics.precision(labels=image_truth, predictions=image_predictions,
                                                          name="image_precision",
                                                          updates_collections=['extra_metrics_ops'])

    tf.summary.scalar('recall_1', recall, collections=["summaries"])
    tf.summary.scalar('recall_per_image', image_recall, collections=["extra_summaries"])
    tf.summary.scalar('precision_1', precision, collections=["summaries"])
    tf.summary.scalar('precision_per_image', image_precision, collections=["extra_summaries"])
    tf.summary.scalar('f1_score', f1_score, collections=["summaries"])

    # Create summary hooks
    tf.summary.scalar('accuracy', accuracy, collections=["summaries"])
    tf.summary.scalar('accuracy_per_image', image_accuracy, collections=["summaries"])
    tf.summary.scalar('cross_entropy', mean_ce, collections=["summaries"])
    tf.summary.scalar('learning_rate', learning_rate, collections=["summaries"])

    # add this so that the batch norm gets run
    extra_update_ops = tf.get_collection(tf.GraphKeys.UPDATE_OPS)

    # collect the metrics ops into one op so we can run that at test time
    metrics_op = tf.get_collection('metrics_ops')

    # Merge all the summaries
    merged = tf.summary.merge_all("summaries")
    kernel_summaries = tf.summary.merge_all("kernels")

    print("Graph created...")

## CONFIGURE OPTIONS
if init_model is not None:
    if os.path.exists(os.path.join("model", init_model + '.ckpt.index')):
        init = False
    else:
        init = True
elif restore_model is not None:
    if os.path.exists(os.path.join("model", restore_model + '.ckpt.index')):
        init = False
    else:
        init = True
else:
    if os.path.exists(os.path.join("model", model_name + '.ckpt.index')):
        init = False
    else:
        init = True

meta_data_every = 1
log_to_tensorboard = True
print_every = 5  # how often to print metrics
checkpoint_every = 1  # how often to save model in epochs
use_gpu = False  # whether or not to use the GPU
print_metrics = True  # whether to print or plot metrics, if False a plot will be created and updated every epoch

# Initialize metrics or load them from disk if they exist
train_acc_values = []
train_cost_values = []
train_lr_values = []
train_recall_values = []
valid_acc_values = []
valid_cost_values = []
valid_recall_values = []

config = tf.ConfigProto()

# if we are freezing some layers adjust the steps per epoch since we will do one extra training step
if freeze:
    steps_per_epoch -= 1

## train the model
with tf.Session(graph=graph, config=config) as sess:
    if log_to_tensorboard:
        train_writer = tf.summary.FileWriter('./logs/tr_' + model_name, sess.graph)
        test_writer = tf.summary.FileWriter('./logs/te_' + model_name)

    # create the saver
    saver = tf.train.Saver()
    sess.run(tf.local_variables_initializer())

    # If the model is new initialize variables, else restore the session
    if init:
        sess.run(tf.global_variables_initializer())
        print("Initializing model...")
    else:
        # if we are initializing with the weights from another model load it
        if init_model is not None:
            # initialize the global variables
            sess.run(tf.global_variables_initializer())

            # create the initializer function to initialize the weights
            # init_fn = load_weights(init_model, exclude=['bottleneck_5.1',"up_conv6", "conv_up_conv6", "bn_up_conv6", "bn_unpool5.1", "up_conv5",'bn_unpool4.1', "up_conv4", "bn_bottleneck_5.1", 'bottleneck_5.2', 'bn_bottleneck_5.2', 'bn_bottleneck_4.1', 'bottleneck_4.2', 'bn_bottleneck_4.2', 'bottleneck_4.1', 'bn_bottleneck_4.1', 'bn_unpool1.1', "up_conv3", "bn_upsample_3", "upsample_3", "up_conv6", "up_conv5", "bn_unpool4.1", 'up_conv4', 'bn_up_conv3', 'up_conv3', 'bn_upsample_2', 'upsample_2', 'bn_bottleneck_4.2', 'bottleneck_4.2', 'bn_bottleneck_4.1', 'bottleneck_4.1', 'bn_bottleneck_5.2', 'bn_bottleneck_5.1', 'bottleneck_5.2', 'bottleneck_5.1', 'bn_upsample_1', 'upsample_1'])
            init_fn = load_weights(init_model, exclude=["upsample_5",'conv5.4',"bn5.4", 'conv5.2',"bn5.2", 'conv5.3',"bn5.3","bn_fc_fc_1","bn_fc_fc_2","fc_fc_1","fc_fc_2",'up_conv3', 'bn_up_conv3', "bn_unpool_4.1", "unpool_4.1", 'upsample_1', 'bn_upsample_1', 'up_conv1', 'bn_up_conv1', 'bottleneck_5.1', 'bn_bottleneck_5.1', 'bottleneck_5.2', 'bn_bottleneck_5.2', 'upsample_2', 'bn_upsample_2', 'up_conv2', 'bn_up_conv2', 'bottleneck_4.1', 'bn_bottleneck_4.1', 'bottleneck_4.2', 'bn_bottleneck_4.2', 'up_conv3', 'bn_up_conv3', 'up_conv4', 'bn_up_conv4', "conv_up_conv5", "bn_up_conv5", "conv_up_conv6", "bn_up_conv6", "conv_up_conv7", "bn_up_conv7", 'upsample_4', 'bn_upsample_4', "logits"])
            # init_fn = load_weights(init_model, exclude=[])

            # init_fn = load_weights(init_model, exclude=['up_conv1', "bn_unpool1.1", "conv_fc_1", "bn_fc_1"])

            # run the initializer
            init_fn(sess)

            # reset the global step
            initial_global_step = global_step.assign(0)
            sess.run(initial_global_step)

            print("Initializing weights from model", init_model)

            # reset init model so we don't do this again
            init_model = None
        elif restore_model is not None:
            saver.restore(sess, './model/' + restore_model + '.ckpt')
            print("Restoring model", restore_model)

            # initial_global_step = global_step.assign(0)
            # sess.run(initial_global_step)

        # otherwise load this model
        else:
            saver.restore(sess, './model/' + model_name + '.ckpt')
            print("Restoring model", model_name)

    # start the queue runners
    coord = tf.train.Coordinator()
    threads = tf.train.start_queue_runners(coord=coord)

    # if we are training the model
    if action == "train":

        print("Training model", model_name, "...")

        for epoch in range(epochs):
            sess.run(tf.local_variables_initializer())

            if freeze:
                train_op = train_op_2
            else:
                train_op = train_op_1

            # Accuracy values (train) after each batch
            batch_acc = []
            batch_cost = []
            batch_recall = []

            for i in range(steps_per_epoch):
                # create the metadata
                run_options = tf.RunOptions(trace_level=tf.RunOptions.FULL_TRACE)
                run_metadata = tf.RunMetadata()

                # Run training op and update ops
                if (i % 50 != 0) or (i == 0):
                    # log the kernel images once per epoch
                    if (i == (steps_per_epoch - 1)) and log_to_tensorboard:
                        _, _, image_summary, step = sess.run(
                            [train_op, extra_update_ops, kernel_summaries, global_step],
                            feed_dict={
                                training: True,
                            },
                            options=run_options,
                            run_metadata=run_metadata)

                        # write the summary
                        train_writer.add_summary(image_summary, step)

                    else:
                        _, _, step = sess.run(
                            [train_op, extra_update_ops, global_step],
                            feed_dict={
                                training: True,
                            },
                            options=run_options,
                            run_metadata=run_metadata)

                # every 50th step get the metrics
                else:
                    _, _, precision_value, summary, acc_value, cost_value, recall_value, step, lr = sess.run(
                        [train_op, extra_update_ops, prec_op, merged, accuracy, mean_ce, rec_op, global_step,
                         learning_rate],
                        feed_dict={
                            training: True,
                        },
                        options=run_options,
                        run_metadata=run_metadata)

                    # Save accuracy (current batch)
                    batch_acc.append(acc_value)
                    batch_cost.append(cost_value)
                    batch_recall.append(recall_value)

                    # log the summaries to tensorboard every 50 steps
                    if log_to_tensorboard:
                        # write the summary
                        train_writer.add_summary(summary, step)

                # only log the meta data once per epoch
                if i == 1:
                    train_writer.add_run_metadata(run_metadata, 'step %d' % step)

            # save checkpoint every nth epoch
            if (epoch % checkpoint_every == 0):
                # if we have frozen some layers run one more iteration on the full training op so we (hopefully) save the entire graph
                if freeze:
                    _ = sess.run(train_op_1, feed_dict={
                        training: True,
                    })

                print("Saving checkpoint")
                save_path = saver.save(sess, './model/' + model_name + '.ckpt')

                # Now that model is saved set init to false so we reload it next time
                init = False

            # init batch arrays
            batch_cv_acc = []
            batch_cv_loss = []
            batch_cv_recall = []

            # initialize the local variables so we have metrics only on the evaluation
            sess.run(tf.local_variables_initializer())

            print("Evaluating model...")
            # load the test data
            X_cv, y_cv = load_validation_data(percentage=1, how=how, which=dataset, scale=True, size=size)
            counter = 0

            # evaluate on pre-cropped images
            for X_batch, y_batch in get_batches(X_cv, y_cv, batch_size, distort=False):
                _, valid_acc, valid_recall, valid_cost = sess.run(
                    [metrics_op, accuracy, recall, mean_ce],
                    feed_dict={
                        X: X_batch,
                        y: y_batch,
                        training: False
                    })

            # one more step to get our metrics
            summary, valid_acc, valid_recall, valid_prec = sess.run(
                [merged, accuracy, recall, precision],
                feed_dict={
                    # X: X_cv[0:2],
                    # y: y_cv[0:2],
                    training: False
                })

            batch_cv_acc.append(valid_acc)
            batch_cv_loss.append(valid_cost)
            batch_cv_recall.append(valid_recall)

            if log_to_tensorboard:
                test_writer.add_summary(summary, step)

            step += 1

            # delete the test data to save memory
            del (X_cv)
            del (y_cv)

            print("Done evaluating...")

            # take the mean of the values to add to the metrics
            valid_acc_values.append(np.mean(batch_cv_acc))
            valid_cost_values.append(np.mean(batch_cv_loss))
            valid_recall_values.append(np.mean(batch_cv_recall))

            train_lr_values.append(lr)

            # Print progress every nth epoch to keep output to reasonable amount
            if (epoch % print_every == 0):
                print(
                    'Epoch {:02d} - step {} - cv acc: {:.4f} - train acc: {:.3f} (mean)'.format(
                        epoch, step, np.mean(batch_cv_acc), np.mean(batch_acc)
                    ))

    # stop the coordinator
    coord.request_stop()

    # Wait for threads to stop
    coord.join(threads)

    sess.run(tf.local_variables_initializer())
    print("Evaluating on test data")

    # evaluate the test data
    X_te, y_te = load_validation_data(how=how, data="test", which=dataset, scale=True, size=size)

    test_accuracy = []
    test_recall = []
    test_predictions = []
    ground_truth = []
    for X_batch, y_batch in get_batches(X_te, y_te, batch_size, distort=False):
        yhat, test_acc_value, test_recall_value, test_prec_value = sess.run([predictions, acc_op, rec_op, prec_op],
                                                                            feed_dict=
                                                                            {
                                                                                X: X_batch,
                                                                                y: y_batch,
                                                                                training: False
                                                                            })

        test_accuracy.append(test_acc_value)
        test_recall.append(test_recall_value)
        test_predictions.append(yhat)
        ground_truth.append(y_batch)

    # print the results
    print("Mean Test Accuracy:", np.mean(test_accuracy))
    print("Mean Test Recall:", np.mean(test_recall))

    # unlist the predictions and truth
    test_predictions = flatten(test_predictions)
    ground_truth = flatten(ground_truth)


