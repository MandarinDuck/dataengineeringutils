import pandas as pd
import os
import re
import numpy as np
import boto3
import json
import pkg_resources
import dataengineeringutils.meta as meta_utils
from dataengineeringutils.datatypes import translate_metadata_type_to_type
from dataengineeringutils.utils import dict_merge, read_json

from io import StringIO
glue_client = boto3.client('glue', 'eu-west-1')
s3_resource = boto3.resource('s3')

import logging
log = logging.getLogger(__name__)

def df_to_csv_s3(df, bucket, path, index=False, header=False):
    """
    Takes a pandas dataframe and writes out to s3
    """
    csv_buffer = StringIO()

    #Skip headers is necessary for now - see here: https://twitter.com/esh/status/811396849756041217
    df.to_csv(csv_buffer, index=index, header=header)

    s3_f = s3_resource.Object(bucket, path)
    response = s3_f.put(Body=csv_buffer.getvalue())


def get_table_definition_template(template_type = 'csv', **kwargs):
    """Get a definition template that can be used with Glue for the various template types

    Args:
        template_type: Allowed values are {'csv', 'parquet', 'avro', 'orc'}
        **kwargs: Arbitrary keyword arguments which will be added to the template
    """
    base_io = pkg_resources.resource_stream(__name__, "specs/base.json")
    base = json.load(base_io)

    conversion = {
        "avro": pkg_resources.resource_stream(__name__, "specs/avro_specific.json"),
        "csv": pkg_resources.resource_stream(__name__, "specs/csv_specific.json"),
        "orc": pkg_resources.resource_stream(__name__, "specs/orc_specific.json"),
        "par": pkg_resources.resource_stream(__name__, "specs/par_specific.json"),
        "parquet": pkg_resources.resource_stream(__name__, "specs/par_specific.json")
        }

    specific_io = conversion[template_type]
    specific = json.load(specific_io)
    dict_merge(base, specific)
    dict_merge(base, kwargs)
    return base


def overwrite_or_create_database(db_name, db_description=""):
    """
    Creates a database in Glue.  If it exists, delete it
    """
    db = {
        "DatabaseInput": {
            "Description": db_description,
            "Name": db_name,
        }
    }

    try:
        glue_client.delete_database(Name=db_name)
        log.debug("Deleting database: {}".format(db_name))
    except :
        pass

    log.debug("Creating database: {}".format(db_name))
    response = glue_client.create_database(**db)

# Add table to database in glue
def create_table_in_glue_from_def(db_name, table_name, table_spec) :
    try :
        glue_client.delete_table(
            DatabaseName=db_name,
            Name=table_name
        )
    except :
        pass

    response = glue_client.create_table(
        DatabaseName=db_name,
        TableInput=table_spec)

# Does what it says on the tin
def take_script_and_run_job(input_script_path, output_script_path, role, job_name, script_bucket = "alpha-dag-data-engineers-raw", temp_dir = "s3://alpha-dag-data-engineers-raw/athena_out"):

    s3_f = s3_resource.Object(script_bucket, output_script_path)
    response= s3_f.put(Body=open(input_script_path, "rb"))

    job = {'AllocatedCapacity': 2,
     'Command': {
      'Name': 'glueetl',
      'ScriptLocation': 's3://{}/{}'.format(script_bucket, output_script_path)
     },
     'DefaultArguments': {'--TempDir': temp_dir,
      '--job-bookmark-option': 'job-bookmark-disable'},
     'ExecutionProperty': {'MaxConcurrentRuns': 1},
     'MaxRetries': 0,
     'Name': job_name,
     'Role': role}

    response = glue_client.create_job(**job)
    response = glue_client.start_job_run(JobName=job_name)

def get_glue_column_spec_from_metadata(metadata):
    """
    Use metadata to create a column spec which will fit into the Glue table template
    """
    columns = metadata["columns"]
    columns.sort(key=lambda x: x["column_number"])
    glue_columns = []
    for c in columns:
        new_c = {}
        new_c["Name"] = c["name"]
        new_c["Comment"] = c["description"]
        new_c["Type"] = translate_metadata_type_to_type(c["type"], "glue")
        glue_columns.append(new_c)
    return glue_columns


def metadata_to_glue_table_definition(metadata):
    """
    Use metadata in json format to create a table definition
    """

    template_type = metadata["data_format"]
    table_definition = get_table_definition_template(template_type)
    column_spec = get_glue_column_spec_from_metadata(metadata)

    table_definition["Name"] = metadata["table_name"]
    table_definition["Description"] = metadata["table_desc"]

    table_definition['StorageDescriptor']['Columns'] = column_spec
    table_definition['StorageDescriptor']["Location"] = metadata["location"]

    return table_definition

def populate_glue_catalogue_from_metadata(table_metadata, db_metadata, check_existence = True):
    """
    Take metadata and make requisite calls to AWS API using boto3
    """

    database_name = db_metadata["name"]
    database_description = ["description"]

    table_name = table_metadata["table_name"]
    tbl_def = metadata_to_glue_table_definition(table_metadata)

    if check_existence:
        try:
            glue_client.get_database(Name=database_name)
        except glue_client.exceptions.EntityNotFoundException:
            overwrite_or_create_database(database_name, db_metadata["description"])

        try:
            glue_client.delete_table(DatabaseName=database_name, Name=table_name)
        except glue_client.exceptions.EntityNotFoundException:
            pass

    return glue_client.create_table(
        DatabaseName=database_name,
        TableInput=tbl_def)


def metadata_folder_to_database(folder_path, delete_db = True):
    """
    Take a metadata folder and build the database and all tables
    """
    files = os.listdir(folder_path)
    files = set([f for f in files if re.match(".+\.json$", f)])

    if "database.json" in files:
        db_metadata = read_json(os.path.join(folder_path, "database.json"))
        database_name = db_metadata["name"]
        try:
            glue_client.delete_database(Name=database_name)
        except glue_client.exceptions.EntityNotFoundException:
            pass
        overwrite_or_create_database(database_name, db_metadata["description"])

    else:
        raise ValueError("database.json not found in metadata folder")
        return None

    table_paths = files.difference({"database.json"})
    for table_path in table_paths:
        table_path = os.path.join(folder_path, table_path)
        table_metadata = read_json(table_path)
        populate_glue_catalogue_from_metadata(table_metadata, db_metadata, check_existence=False)