from __future__ import print_function, division
from Queue import Queue
import threading
import numpy as np
import pandas as pd
from nilmtk import DataSet, TimeFrame
from datetime import timedelta

class Source(threading.Thread):
    def __init__(self, seq_length, n_seq_per_batch, n_inputs, n_outputs):
        super(Source, self).__init__()
        self.seq_length = seq_length
        self.n_seq_per_batch = n_seq_per_batch
        self.n_inputs = n_inputs
        self.n_outputs = n_outputs
        self.queue = Queue(maxsize=2)
        self._stop = threading.Event()
        
    def run(self):
        """Puts training data into a Queue"""
        self._stop.clear()
        while not self._stop.is_set():
            self.queue.put(self._gen_data())
            
    def stop(self):
        self.queue.get()
        self._stop.set()
        
    def validation_data(self):
        return self._gen_data(validation=True)

    def _gen_data(self, validation=False):
        raise NotImplementedError()

    def input_shape(self):
        return (self.n_seq_per_batch, 
                self.seq_length, 
                self.n_inputs)

    def output_shape(self):
        return (self.n_seq_per_batch, 
                self.seq_length, 
                self.n_outputs)

    def _check_data(self, X, y):
        assert X.shape == self.input_shape()
        if y is not None:
            assert y.shape == self.output_shape()


def none_to_list(x):
    return [] if x is None else x


class ToySource(Source):
    def __init__(self, seq_length, n_seq_per_batch, n_inputs=1,
                 powers=None, on_durations=None, all_hot=True):
        """
        Parameters
        ----------
        n_inputs : int
            if > 1 then will quantize inputs
        powers : list of numbers
        on_durations : list of numbers
        """
        super(ToySource, self).__init__(
            seq_length=seq_length, 
            n_seq_per_batch=n_seq_per_batch,
            n_inputs=n_inputs, 
            n_outputs=1)
        self.powers = [10,40] if powers is None else powers
        self.on_durations = [3,10] if on_durations is None else on_durations
        self.all_hot = all_hot

    def _gen_single_appliance(self, power, on_duration, 
                              min_off_duration=20, fdiff=True, p=0.2):
        length = self.seq_length + 1 if fdiff else self.seq_length
        appliance_power = np.zeros(shape=(length, 1))
        i = 0
        while i < length:
            if np.random.binomial(n=1, p=p):
                end = min(i + on_duration, length)
                appliance_power[i:end] = power
                i += on_duration + min_off_duration
            else:
                i += 1
        return np.diff(appliance_power) if fdiff else appliance_power

    def _gen_batches_of_single_appliance(self, *args, **kwargs):
        batches = np.empty(shape=(self.n_seq_per_batch, self.seq_length, 1))
        for i in range(self.n_seq_per_batch):
            batches[i, :, :] = self._gen_single_appliance(*args, **kwargs)
        return batches

    def gen_unquantized_data(self, validation=False):
        y = self._gen_batches_of_single_appliance(
            power=self.powers[0], on_duration=self.on_durations[0])
        X = y.copy()
        for power, on_duration in zip(self.powers, self.on_durations)[1:]:
            X += self._gen_batches_of_single_appliance(
                power=power, on_duration=on_duration)

        max_power = np.sum(self.powers)
        return X / max_power, y / max_power

    def gen_data(self, *args, **kwargs):
        X, y = self.gen_unquantized_data(*args, **kwargs)
        if self.n_inputs > 1:
            X = quantize(X, self.n_inputs, self.all_hot)
        return X, y


class NILMTKSource(Source):
    def __init__(self, filename, appliances, building=1):
        """
        Parameters
        ----------
        filename : str
        appliances : list of strings
            The first one is the target appliance
        building : int
        """
        super(NILMTKSource, self).__init__(
            seq_length=14400, 
            n_seq_per_batch=5,
            n_inputs=1000, 
            n_outputs=1)
        self.sample_period = 6
        self.min_power =  20
        self.max_power = 200
        self.dataset = DataSet(filename)
        self.appliances = appliances
        self._tz = self.dataset.metadata['timezone']
        self.metergroup = self.dataset.buildings[building].elec

    def _get_data_for_single_day(self, start):
        start = pd.Timestamp(start).date()
        end = start + timedelta(days=1)
        timeframe = TimeFrame(start, end, tz=self._tz)
        load_kwargs = dict(sample_period=self.sample_period, 
                           sections=[timeframe])

        # Load output (target) data
        y = self.metergroup[self.appliances[0]].power_series_all_data(**load_kwargs)
        if y is None or y.max() < self.min_power:
            return None, None

        # Load input (aggregate) data
        X = y + self.metergroup[self.appliances[1]].power_series_all_data(**load_kwargs)
        for appliance in self.appliances[2:]:
            X += self.metergroup[appliance].power_series_all_data(**load_kwargs)

        freq = "{:d}S".format(self.sample_period)
        index = pd.date_range(start, end, freq=freq, tz=self._tz)
        def preprocess(data):
            data = data.fillna(0)
            data = data.clip(upper=self.max_power)
            data[data < self.min_power] = 0
            data = data.reindex(index, fill_value=0)
            data /= self.max_power
            return data

        def index_as_minus_one_to_plus_one(data):
            index = data.index.astype(np.int64)
            index -= np.min(index)
            index = index.astype(np.float32)
            index /= np.max(index)
            return np.vstack([index, data.values]).transpose()

        X = preprocess(X).diff().dropna().values
        y = preprocess(y).diff().dropna().values
        return X, y

    def _gen_unquantized_data(self, validation=False):
        X = np.empty(shape=(self.n_seq_per_batch, self.seq_length, 1))
        y = np.empty(shape=self.output_shape())
        N_DAYS = 600 # there are more like 632 days in the dataset
        FIRST_DAY = pd.Timestamp("2013-04-12")
        seq_i = 0
        while seq_i < self.n_seq_per_batch:
            if validation:
                days = np.random.randint(low=N_DAYS, high=N_DAYS + self.n_seq_per_batch)
            else:
                days = np.random.randint(low=0, high=N_DAYS)
            start = FIRST_DAY + timedelta(days=days)
            X_one_seq, y_one_seq = self._get_data_for_single_day(start)

            if y_one_seq is not None:
                try:
                    X[seq_i,:,:] = X_one_seq.reshape(self.seq_length, 1)
                    y[seq_i,:,:] = y_one_seq.reshape(self.seq_length, 1)
                except ValueError as e:
                    print(e)
                    print("Skipping", start)
                else:
                    seq_i += 1
            else:
                print("Skipping", start)
        return X, y

    def _gen_data(self, *args, **kwargs):
        X = kwargs.pop('X', None)
        if X is None:
            X, y = self._gen_unquantized_data(*args, **kwargs)
        else:
            y = None
        X_quantized = np.empty(shape=self.input_shape())
        for i in range(self.n_seq_per_batch):
            X_quantized[i,:,0] = X[i,:,0] # time of day
            X_quantized[i,:,1:] = quantize(X[i,:,1], self.n_inputs)

        self._check_data(X_quantized, y)
        return X_quantized, y


def quantize(data, n_bins, all_hot=True):
    midpoint = n_bins // 2
    out = np.empty(shape=(len(data), n_bins))
    for i, d in enumerate(data):
        hist, _ = np.histogram(d, bins=n_bins, range=(-1, 1))
        if all_hot:
            where = np.where(hist==1)[0][0]
            if where > midpoint:
                hist[midpoint:where] = 1
            elif where < midpoint:
                hist[where:midpoint] = 1
        out[i,:] = hist
    return (out * 2) - 1
