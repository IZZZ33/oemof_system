import os
import shutil
import numpy as np
import pandas as pd
from pandas.plotting import register_matplotlib_converters
register_matplotlib_converters()


def create_folder(path):
    if not os.path.exists(path):
        os.makedirs(path)
        print(path + ' - created')


def check_and_move_folder(src, dst):
    if os.path.exists(src):
        shutil.move(src, dst)
        print(src + ' - moved to ' + dst)


def remove_folder(path):
    shutil.rmtree(path)
    print(path + ' - cleaned')


def get_info_from_excel_data(xls_data, label):
    """
    returns dict with all column names and cell data for the row of node label.
    if there are entries with the same label (which should not be!) only the first found row with label is returned
    """
    # iterate through sheets
    for sheet in xls_data.sheet_names:
        sheet_data = xls_data.parse(sheet)

        # skip sheets were no label column exists
        if 'label' in sheet_data.columns:
            # search rows for the label
            for i, b in sheet_data.iterrows():
                if b['label'] == label:
                    # write all row data into dict
                    data_dict = {}
                    for col in sheet_data.columns:
                        data_dict[col] = sheet_data.loc[i, col]
                    return data_dict
                    # for col in sheet_data.columns:
    return None


def convert_second_columns_to_datetimeindex(df, time_column, start_date):
    """Create DateTimeIndex data frame from column in seconds"""
    start_date = np.datetime64(start_date)
    time = start_date + df[time_column].astype(dtype='timedelta64[s]')
    df.index = pd.DatetimeIndex(time)
    df = df.drop(time_column, axis=1)
    return df


def convert_data_frames(df, time, datetimeindex):
    """Returns a new DataFrame with all columns interpolated and new index"""
    df_out = interp(df, time)
    # df_out['Time/s'] = df_out.index
    df_out = df_out.set_index(datetimeindex, drop=False)
    return df_out


def interp(df, new_index):
    """Return a new DataFrame with all columns values interpolated
    to the new_index values."""
    df_out = pd.DataFrame(index=new_index)
    df_out.index.name = df.index.name

    for colname, col in df.iteritems():
        df_out[colname] = np.interp(new_index.astype(float), df.index, col)
    return df_out
