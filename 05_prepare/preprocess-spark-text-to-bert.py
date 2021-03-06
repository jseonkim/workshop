from __future__ import print_function
from __future__ import unicode_literals

import time
import sys
import os
import shutil
import csv

import subprocess
import sys
subprocess.check_call([sys.executable, '-m', 'pip', 'install', 'tensorflow==2.1.0'])
import tensorflow as tf
print(tf.__version__)
subprocess.check_call([sys.executable, '-m', 'pip', 'install', 'transformers==2.8.0'])
from transformers import DistilBertTokenizer

import pyspark
from pyspark.sql import SparkSession
from pyspark.ml import Pipeline
from pyspark.sql.types import StructField, StructType, StringType, IntegerType, DateType
from pyspark.sql.functions import *
from pyspark.ml.linalg import DenseVector
from pyspark.sql.functions import split
from pyspark.sql.functions import udf, col
from pyspark.sql.types import ArrayType, DoubleType

tokenizer = DistilBertTokenizer.from_pretrained('distilbert-base-uncased')

# We set sequences to be at most 128 tokens long.
MAX_SEQ_LENGTH = 128
DATA_COLUMN = 'review_body'
LABEL_COLUMN = 'star_rating'
LABEL_VALUES = [1, 2, 3, 4, 5]

label_map = {}
for (i, label) in enumerate(LABEL_VALUES):
    label_map[label] = i


class InputFeatures(object):
  """BERT feature vectors."""

  def __init__(self,
               input_ids,
               input_mask,
               segment_ids,
               label_id):
    self.input_ids = input_ids
    self.input_mask = input_mask
    self.segment_ids = segment_ids
    self.label_id = label_id


class Input(object):
  """A single training/test input for sequence classification."""

  def __init__(self, text, label=None):
    """Constructs an Input.
    Args:
      text: string. The untokenized text of the first sequence. For single
        sequence tasks, only this sequence must be specified.
      label: (Optional) string. The label of the example. This should be
        specified for train and dev examples, but not for test examples.
    """
    self.text = text
    self.label = label
    

def convert_input(text_input):
    # First, we need to preprocess our data so that it matches the data BERT was trained on:
    #
    # 1. Lowercase our text (if we're using a BERT lowercase model)
    # 2. Tokenize it (i.e. "sally says hi" -> ["sally", "says", "hi"])
    # 3. Break words into WordPieces (i.e. "calling" -> ["call", "##ing"])
    #
    # Fortunately, the Transformers tokenizer does this for us!
    #
    tokens = tokenizer.tokenize(text_input.text)

    # Next, we need to do the following:
    #
    # 4. Map our words to indexes using a vocab file that BERT provides
    # 5. Add special "CLS" and "SEP" tokens (see the [readme](https://github.com/google-research/bert))
    # 6. Append "index" and "segment" tokens to each input (see the [BERT paper](https://arxiv.org/pdf/1810.04805.pdf))
    #
    # Again, the Transformers tokenizer does this for us!
    #
    encode_plus_tokens = tokenizer.encode_plus(text_input.text,
                                               pad_to_max_length=True,
                                               max_length=MAX_SEQ_LENGTH)

    # Convert the text-based tokens to ids from the pre-trained BERT vocabulary
    input_ids = encode_plus_tokens['input_ids']
    # Specifies which tokens BERT should pay attention to (0 or 1)
    input_mask = encode_plus_tokens['attention_mask']
    # Segment Ids are always 0 for single-sequence tasks (or 1 if two-sequence tasks)
    segment_ids = [0] * MAX_SEQ_LENGTH

    # Label for our training data (star_rating 1 through 5)
    label_id = label_map[text_input.label]

    features = InputFeatures(
        input_ids=input_ids,
        input_mask=input_mask,
        segment_ids=segment_ids,
        label_id=label_id)

#    print('**tokens**\n{}\n'.format(tokens))
#    print('**input_ids**\n{}\n'.format(features.input_ids))
#    print('**input_mask**\n{}\n'.format(features.input_mask))
#    print('**segment_ids**\n{}\n'.format(features.segment_ids))
#    print('**label_id**\n{}\n'.format(features.label_id))

    return features


def convert_features_to_tfrecord(inputs,
                                 output_file):
    """Convert a set of `Input`s to a TFRecord file."""

    tfrecord_writer = tf.io.TFRecordWriter(output_file)

    for (input_idx, text_input) in enumerate(inputs):
        if input_idx % 1000 == 0:
            print("Writing example %d of %d" % (input_idx, len(inputs)))

            bert_features = convert_input(text_input)

            tfrecord_features = collections.OrderedDict()

            tfrecord_features['input_ids'] = tf.train.Feature(int64_list=tf.train.Int64List(value=bert_features.input_ids))
            tfrecord_features['input_mask'] = tf.train.Feature(int64_list=tf.train.Int64List(value=bert_features.input_mask))
            tfrecord_features['segment_ids'] = tf.train.Feature(int64_list=tf.train.Int64List(value=bert_features.segment_ids))
            tfrecord_features['label_ids'] = tf.train.Feature(int64_list=tf.train.Int64List(value=[bert_features.label_id]))

            tfrecord = tf.train.Example(features=tf.train.Features(feature=tfrecord_features))

            tfrecord_writer.write(tfrecord.SerializeToString())

    tfrecord_writer.close()
    
    
def list_arg(raw_value):
    """argparse type for a list of strings"""
    return str(raw_value).split(',')


def parse_args():
    # Unlike SageMaker training jobs (which have `SM_HOSTS` and `SM_CURRENT_HOST` env vars), processing jobs to need to parse the resource config file directly
    resconfig = {}
    try:
        with open('/opt/ml/config/resourceconfig.json', 'r') as cfgfile:
            resconfig = json.load(cfgfile)
    except FileNotFoundError:
        print('/opt/ml/config/resourceconfig.json not found.  current_host is unknown.')
        pass # Ignore

    # Local testing with CLI args
    parser = argparse.ArgumentParser(description='Process')

    parser.add_argument('--hosts', type=list_arg,
        default=resconfig.get('hosts', ['unknown']),
        help='Comma-separated list of host names running the job'
    )
    parser.add_argument('--current-host', type=str,
        default=resconfig.get('current_host', 'unknown'),
        help='Name of this host running the job'
    )
    parser.add_argument('--input-data', type=str,
        default='/opt/ml/processing/input/data',
    )
    parser.add_argument('--output-data', type=str,
        default='/opt/ml/processing/output',
    )
    return parser.parse_args()


def _transform_tsv_to_tfrecord(df):
#    print(file)

#    filename_without_extension = Path(Path(file).stem).stem

#    df = pd.read_csv(file,
#                     delimiter='\t',
#                     quoting=csv.QUOTE_NONE,
#                     compression='gzip')

    df.isna().values.any()
    df = df.dropna()
    df = df.reset_index(drop=True)

    print('Shape of dataframe {}'.format(df.shape))

    # Balance to the minority class (star_rating 2 in our overall dataset)
    from sklearn.utils import resample

    five_star_df = df.query('star_rating == 5')
    four_star_df = df.query('star_rating == 4')
    three_star_df = df.query('star_rating == 3')
    two_star_df = df.query('star_rating == 2')
    one_star_df = df.query('star_rating == 1')

    five_star_df = resample(five_star_df,
                            replace = False,
                            n_samples = len(two_star_df),
                            random_state = 27)

    four_star_df = resample(four_star_df,
                            replace = False,
                            n_samples = len(two_star_df),
                            random_state = 27)

    three_star_df = resample(three_star_df,
                            replace = False,
                            n_samples = len(two_star_df),
                            random_state = 27)

    two_star_df = resample(two_star_df,
                            replace = False,
                            n_samples = len(two_star_df),
                            random_state = 27)

    one_star_df = resample(one_star_df,
                            replace = False,
                            n_samples = len(two_star_df),
                            random_state = 27)

    df_balanced = pd.concat([five_star_df, four_star_df, three_star_df, two_star_df, one_star_df])
    df_balanced = df_balanced.reset_index(drop=True)
    print('Shape of balanced dataframe {}'.format(df_balanced.shape))

    # HACK - Running on just 2 files, not balancing, and training without steps_per_epoch
#    df_balanced = df

    # Split all data into 90% train and 10% holdout
    df_train, df_holdout = train_test_split(df_balanced, test_size=0.10, stratify=df_balanced['star_rating'])
    # Split holdout data into 50% validation and 50% test
    df_validation, df_test = train_test_split(df_holdout, test_size=0.50, stratify=df_holdout['star_rating'])

    df_train = df_train.reset_index(drop=True)
    df_validation = df_validation.reset_index(drop=True)
    df_test = df_test.reset_index(drop=True)

    print('Shape of train dataframe {}'.format(df_train.shape))
    print('Shape of validation dataframe {}'.format(df_validation.shape))
    print('Shape of test dataframe {}'.format(df_test.shape))

    train_inputs = df_train.apply(lambda x: Input(text = x[DATA_COLUMN],
                                                         label = x[LABEL_COLUMN]), axis = 1)
    
    validation_inputs = df_validation.apply(lambda x: Input(text = x[DATA_COLUMN],
                                                            label = x[LABEL_COLUMN]), axis = 1)

    test_inputs = df_test.apply(lambda x: Input(text = x[DATA_COLUMN],
                                                label = x[LABEL_COLUMN]), axis = 1)

    # Next, we need to preprocess our data so that it matches the data BERT was trained on. For this, we'll need to do a coupleof things (but don't worry--this is also included in the Python library):
    #
    #
    # 1. Lowercase our text (if we're using a BERT lowercase model)
    # 2. Tokenize it (i.e. "sally says hi" -> ["sally", "says", "hi"])
    # 3. Break words into WordPieces (i.e. "calling" -> ["call", "##ing"])
    # 4. Map our words to indexes using a vocab file that BERT provides
    # 5. Add special "CLS" and "SEP" tokens (see the [readme](https://github.com/google-research/bert))
    # 6. Append "index" and "segment" tokens to each input (see the [BERT paper](https://arxiv.org/pdf/1810.04805.pdf))
    #
    # We don't have to worry about these details.  The Transformers tokenizer does this for us.
    #
    train_data = '{}/bert/train'.format(args.output_data)
    validation_data = '{}/bert/validation'.format(args.output_data)
    test_data = '{}/bert/test'.format(args.output_data)

    # Convert our train and validation features to InputFeatures (.tfrecord protobuf) that works with BERT and TensorFlow.
    df_train_embeddings = convert_features_to_tfrecord(train_inputs, '{}/part-{}-{}.tfrecord'.format(train_data, args.current_host, filename_without_extension))

    df_validation_embeddings = convert_features_to_tfrecord(validation_inputs, '{}/part-{}-{}.tfrecord'.format(validation_data,args.current_host, filename_without_extension))

    df_test_embeddings = convert_features_to_tfrecord(test_inputs, '{}/part-{}-{}.tfrecord'.format(test_data, args.current_host, filename_without_extension))
    
    
# def process(args):
#     print('Current host: {}'.format(args.current_host))

#     train_data = None
#     validation_data = None
#     test_data = None

#     transform_tsv_to_tfrecord = functools.partial(_transform_tsv_to_tfrecord)
#     input_files = glob.glob('{}/*.tsv.gz'.format(args.input_data))

#     num_cpus = multiprocessing.cpu_count()
#     print('num_cpus {}'.format(num_cpus))

#     p = multiprocessing.Pool(num_cpus)
#     p.map(transform_tsv_to_tfrecord, input_files)

#     print('Listing contents of {}'.format(args.output_data))
#     dirs_output = os.listdir(args.output_data)
#     for file in dirs_output:
#         print(file)

#     print('Listing contents of {}'.format(train_data))
#     dirs_output = os.listdir(train_data)
#     for file in dirs_output:
#         print(file)

#     print('Listing contents of {}'.format(validation_data))
#     dirs_output = os.listdir(validation_data)
#     for file in dirs_output:
#         print(file)

#     print('Listing contents of {}'.format(test_data))
#     dirs_output = os.listdir(test_data)
#     for file in dirs_output:
#         print(file)

#     print('Complete')
    

def transform(spark, s3_input_data, s3_output_train_data, s3_output_validation_data, s3_output_test_data): 
    print('Processing {} => {}'.format(s3_input_data, s3_output_train_data, s3_output_validation_data, s3_output_test_data))
 
    schema = StructType([
        StructField('marketplace', StringType(), True),
        StructField('customer_id', StringType(), True),
        StructField('review_id', StringType(), True),
        StructField('product_id', StringType(), True),
        StructField('product_parent', StringType(), True),
        StructField('product_title', StringType(), True),
        StructField('product_category', StringType(), True),
        StructField('star_rating', IntegerType(), True),
        StructField('helpful_votes', IntegerType(), True),
        StructField('total_votes', IntegerType(), True),
        StructField('vine', StringType(), True),
        StructField('verified_purchase', StringType(), True),
        StructField('review_headline', StringType(), True),
        StructField('review_body', StringType(), True),
        StructField('review_date', StringType(), True)
    ])
    
    df_csv = spark.read.csv(path=s3_input_data,
                            sep='\t',
                            schema=schema,
                            header=True,
                            quote=None)
    df_csv.show()

    # This dataset should already be clean, but always good to double-check
    print('Showing null review_body rows...')
    df_csv.where(col('review_body').isNull()).show()

    df_csv_cleaned = df_csv.na.drop(subset=['review_body'])
    df_csv_cleaned.where(col('review_body').isNull()).show()

    # TODO:  Balance
    

#     tokenizer = Tokenizer(inputCol='review_body', outputCol='words')
#     wordsData = tokenizer.transform(df_csv_cleaned)
    
#     hashingTF = HashingTF(inputCol='words', outputCol='raw_features', numFeatures=1000)
#     featurizedData = hashingTF.transform(wordsData)
    
#     # While applying HashingTF only needs a single pass to the data, applying IDF needs two passes:
#     # 1) compute the IDF vector 
#     # 2) scale the term frequencies by IDF
#     # Therefore, we cache the result of the HashingTF transformation above to speed up the 2nd pass
#     featurizedData.cache()

#     # spark.mllib's IDF implementation provides an option for ignoring terms
#     # which occur in less than a minimum number of documents.
#     # In such cases, the IDF for these terms is set to 0.
#     # This feature can be used by passing the minDocFreq value to the IDF constructor.
#     idf = IDF(inputCol='raw_features', outputCol='features') #, minDocFreq=2)
#     idfModel = idf.fit(featurizedData)

#    features_df = idfModel.transform(featurizedData)
#    features_df.select('star_rating', 'features').show()

#     num_features=300
#     pca = PCA(k=num_features, inputCol='features', outputCol='pca_features')
#     pca_model = pca.fit(features_df)
#     pca_features_df = pca_model.transform(features_df).select('star_rating', 'pca_features')
#     pca_features_df.show(truncate=False)

#     standard_scaler = StandardScaler(inputCol='pca_features', outputCol='scaled_pca_features')
#     standard_scaler_model = standard_scaler.fit(pca_features_df)
#     standard_scaler_features_df = standard_scaler_model.transform(pca_features_df).select('star_rating', 'scaled_pca_features')
#     standard_scaler_features_df.show(truncate=False)

#     expanded_features_df = (standard_scaler_features_df.withColumn('f', to_array(col('scaled_pca_features')))
#         .select(['star_rating'] + [col('f')[i] for i in range(num_features)]))
#     expanded_features_df.show()

    features_df = df_csv_cleaned.select(['star_rating', 'review_body'])

    # TODO:  Convert to TFRecord
    bert_transformer = udf(lambda str: tokenizer.encode_plus(str, 
                                                             pad_to_max_length=True,
                                                             max_length=MAX_SEQ_LENGTH),
			   StringType())
    spark.udf.register('bert_transformer', bert_transformer)

    transformed_df = features_df.withColumn('star_rating', bert_transformer('review_body'))

    # TODO:  Split
    train_df, validation_df, test_df = transformed_df.randomSplit([0.9, 0.05, 0.05])

    # TODO:  Potentially use TFRecord Writer from LI
    train_df.write.csv(path=s3_output_train_data,
                       header=None,
                       quote=None)
    print('Wrote to output file:  {}'.format(s3_output_train_data))

    validation_df.write.csv(path=s3_output_validation_data,
                            header=None,
                            quote=None)
    print('Wrote to output file:  {}'.format(s3_output_validation_data))

    test_df.write.csv(path=s3_output_test_data,
                      header=None,
                      quote=None) 
    print('Wrote to output file:  {}'.format(s3_output_test_data))


def main():
    spark = SparkSession.builder.appName('AmazonReviewsSparkProcessor').getOrCreate()

    # Convert command line args into a map of args
    args_iter = iter(sys.argv[1:])
    args = dict(zip(args_iter, args_iter))

    # Retrieve the args and replace 's3://' with 's3a://' (used by Spark)
    s3_input_data = args['s3_input_data'].replace('s3://', 's3a://')
    print(s3_input_data)

    s3_output_train_data = args['s3_output_train_data'].replace('s3://', 's3a://')
    print(s3_output_train_data)

    s3_output_validation_data = args['s3_output_validation_data'].replace('s3://', 's3a://')
    print(s3_output_validation_data)

    s3_output_test_data = args['s3_output_test_data'].replace('s3://', 's3a://')
    print(s3_output_test_data)

    transform(spark, s3_input_data, s3_output_train_data, s3_output_validation_data, s3_output_test_data)


if __name__ == "__main__":
    main()
