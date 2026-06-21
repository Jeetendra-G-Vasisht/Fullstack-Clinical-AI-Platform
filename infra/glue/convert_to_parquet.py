"""
kBuddhi AI — Glue PySpark Job: convert_to_parquet.py
------------------------------------------------------
Converts CSV and Excel files uploaded to:
    s3://{SOURCE_BUCKET}/uploads/user_id={sub}/year={YYYY}/month={MM}/{filename}

into Parquet files written to:
    s3://{SOURCE_BUCKET}/parquet/user_id={sub}/year={YYYY}/month={MM}/{filename}.parquet

The output partition structure mirrors the input so Athena can query by
user/year/month using Glue Data Catalog partitions.

Triggered on-demand via Lambda after each upload — NOT scheduled.
The Glue job is invoked with these job parameters:
    --SOURCE_KEY  : full S3 key of the uploaded file
    --SOURCE_BUCKET : bucket name (set as default arg in Glue job definition)
    --PARQUET_PREFIX : "parquet/" (set as default arg)

Usage (manual test run via AWS CLI):
    aws glue start-job-run \\
      --job-name kbuddhiai-convert-to-parquet \\
      --arguments '{"--SOURCE_KEY":"uploads/user_id=.../year=2026/month=06/data.csv"}'
"""

import sys
import os

from awsglue.context import GlueContext
from awsglue.job import Job
from awsglue.utils import getResolvedOptions
from pyspark.context import SparkContext
from pyspark.sql import functions as F

args = getResolvedOptions(
    sys.argv,
    ["JOB_NAME", "SOURCE_BUCKET", "PARQUET_PREFIX"],
)

# SOURCE_KEY is optional — if absent, convert all unprocessed files
source_key = None
if "--SOURCE_KEY" in sys.argv:
    source_key = getResolvedOptions(sys.argv, ["SOURCE_KEY"])["SOURCE_KEY"]

sc          = SparkContext()
glueContext = GlueContext(sc)
spark       = glueContext.spark_session
job         = Job(glueContext)
job.init(args["JOB_NAME"], args)

SOURCE_BUCKET  = args["SOURCE_BUCKET"]
PARQUET_PREFIX = args["PARQUET_PREFIX"]

def s3_path(key: str) -> str:
    return f"s3://{SOURCE_BUCKET}/{key}"

def derive_output_key(input_key: str) -> str:
    """
    uploads/user_id=abc/year=2026/month=06/data.csv
    → parquet/user_id=abc/year=2026/month=06/data.csv.parquet
    """
    without_uploads = input_key.removeprefix("uploads/")
    return f"{PARQUET_PREFIX}{without_uploads}.parquet"

def convert_file(input_key: str):
    input_path  = s3_path(input_key)
    output_key  = derive_output_key(input_key)
    output_path = s3_path(output_key)
    filename    = input_key.split("/")[-1]
    ext         = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""

    print(f"Converting: {input_path} → {output_path}")

    try:
        if ext in ("csv", "txt"):
            df = spark.read.option("header", "true").option("inferSchema", "true").csv(input_path)
        elif ext in ("xlsx", "xls"):
            # Spark doesn't natively read Excel — use the com.crealytics package
            # (bundled via Glue's native Excel support in Glue 4.0)
            df = spark.read \
                .format("com.crealytics.spark.excel") \
                .option("useHeader", "true") \
                .option("inferSchema", "true") \
                .load(input_path)
        else:
            # For binary/unsupported types, store as single-column text
            df = spark.createDataFrame(
                [(filename, "[non-tabular file — text extraction done at query time]")],
                ["filename", "content"],
            )

        # Add provenance columns
        df = df \
            .withColumn("_source_key", F.lit(input_key)) \
            .withColumn("_filename",   F.lit(filename))

        df.write \
            .mode("overwrite") \
            .parquet(output_path)

        print(f"Written {df.count()} rows to {output_path}")

    except Exception as e:
        print(f"ERROR converting {input_key}: {e}")
        raise

if source_key:
    convert_file(source_key)
else:
    # Batch mode: convert all CSV/Excel files under uploads/ that don't yet
    # have a corresponding parquet file. Used for backfilling.
    from pyspark.sql import Row
    bucket_df = spark.read.format("binaryFile") \
        .option("recursiveFileLookup", "true") \
        .load(f"s3://{SOURCE_BUCKET}/uploads/")

    keys_to_convert = [
        row["path"].removeprefix(f"s3://{SOURCE_BUCKET}/")
        for row in bucket_df.select("path").collect()
        if row["path"].split(".")[-1].lower() in ("csv", "txt", "xlsx", "xls")
    ]

    for key in keys_to_convert:
        convert_file(key)

job.commit()
