import pandas as pd
import os
import pkg_resources

def translate_metadata_type_to_type(column_type, target_type="glue"):

    io = pkg_resources.resource_stream(__name__, "data/data_type_conversion.csv")
    df = pd.read_csv(io)
    df = df.set_index("metadata")
    lookup = df.to_dict(orient="index")

    try:
        return lookup[column_type][target_type]
    except:
        raise KeyError("You attempted to lookup column type {}, but this cannot be found in data_type_conversion.csv".format(column_type))