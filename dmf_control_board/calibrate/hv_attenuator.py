# coding: utf-8
import pandas as pd
import numpy as np
import sympy as sp
import matplotlib.pyplot as plt
from matplotlib.markers import MarkerStyle
import scipy.optimize as optimize

from functools32 import lru_cache
from microdrop_utility import Version, is_float
from .feedback import compute_from_transfer_function


def measure_board_rms(control_board, n_samples=10, sampling_ms=10,
                      delay_between_samples_ms=0):
    '''
    Read RMS voltage samples from control board high-voltage feedback circuit.
    '''
    results = control_board.measure_impedance(n_samples, sampling_ms,
                                              delay_between_samples_ms, True,
                                              True, [])
    data = pd.DataFrame({'board measured V': results.V_hv})
    data['divider resistor index'] = results.hv_resistor
    return data


def find_good(control_board, actuation_steps, resistor_index, start_index,
              end_index):
    '''
    Use a binary search over the range of provided actuation_steps to find the
    maximum actuation voltage that is measured by the board feedback circuit
    using the specified feedback resistor.
    '''
    lower = start_index
    upper = end_index
    while lower < upper - 1:
        index = lower + (upper - lower) / 2
        v = actuation_steps[index]
        control_board.set_waveform_voltage(v)
        data = measure_board_rms(control_board)
        valid_data = data[data['divider resistor index'] >= 0]

        if (valid_data['divider resistor index'] <
                resistor_index).sum():
            # We have some measurements from another resistor.
            upper = index
        else:
            lower = index
    control_board.set_waveform_voltage(actuation_steps[lower])
    data = measure_board_rms(control_board)
    return lower, data


def resistor_max_actuation_readings(control_board, frequencies,
                                    oscope_reading_func):
    '''
    For each resistor in the high-voltage feedback resistor bank, read the
    board measured voltage and the oscilloscope measured voltage for an
    actuation voltage that nearly saturates the feedback resistor.

    By searching for an actuation voltage near saturation, the signal-to-noise
    ratio is minimized.
    '''
    # Set board amplifier gain to 1.
    # __NB__ This is likely _far_ lower than the actual gain _(which may be a
    # factor of several hundred)_..
    control_board.set_waveform_voltage(0)
    control_board.auto_adjust_amplifier_gain = False
    control_board.amplifier_gain = 1.

    # Set waveform voltage to a low value and obtain the corresponding
    # oscilloscope reading to calculate an approximate gain of the amplifier.
    target_voltage = 0.1
    control_board.set_waveform_voltage(target_voltage)
    oscope_rms = oscope_reading_func()
    estimated_amplifier_gain = oscope_rms / target_voltage

    # Based on the maximum amplified RMS voltage, define a set of actuation
    # voltages to search when performing calibration.
    max_post_gain_V = 0.8 * control_board.max_waveform_voltage
    max_actuation_V = max_post_gain_V / estimated_amplifier_gain
    actuation_steps = np.linspace(0.005, max_actuation_V, num=100)

    # Define frequency/resistor index pairs to take measurements at.
    conditions = pd.DataFrame([[r, f] for r in range(2, -1, -1)
                               for f in frequencies],
                              columns=['resistor index', 'frequency'])

    # Define function to process each frequency/resistor index pair.
    def max_actuation_reading(x):
        '''
        Measure maximum board RMS voltage using specified feedback resistor, at
        the specified frequency.

        Request corresponding oscilloscope RMS voltage reading.
        '''
        r = x['resistor index'].values[0]
        f = x['frequency'].values[0]
        control_board.set_waveform_frequency(f)

        actuation_index, data = find_good(control_board, actuation_steps, r, 0,
                                          len(actuation_steps) - 1)
        board_measured_rms = data.loc[data['divider resistor index']
                                    >= 0, 'board measured V'].mean()
        oscope_rms = oscope_reading_func()
        print 'R=%s, f=%s' % (r, f)
        return pd.DataFrame([[r, f, actuation_index, board_measured_rms,
                            oscope_rms]],
                            columns=['resistor index', 'frequency',
                                     'actuation index', 'board measured V',
                                     'oscope measured V'])

    # Return board-measured RMS voltage and oscilloscope-measured RMS voltage
    # for each frequency/feedback resistor pair.
    return (conditions.groupby(['resistor index', 'frequency'])
            .apply(max_actuation_reading).reset_index(drop=True))


def fit_feedback_params(calibration, max_resistor_readings):
    '''
    Fit model of control board high-voltage feedback resistor and
    parasitic capacitance values based on measured voltage readings.
    '''
    R1 = 10e6

    # Get transfer function to compute the amplitude of the high-voltage input
    # to the control board _(i.e., the output of the amplifier)_ based on the
    # attenuated voltage measured by the analog-to-digital converter on the
    # control board.
    #
    # The signature of the transfer function is:
    #
    #     H(V1, R1, C1, R2, C2, f)
    #
    # See the `z_transfer_functions` function docstring for definitions of the
    # parameters based on the control board major version.
    e = lambda p, freq, y, R1: \
        compute_from_transfer_function(calibration.hw_version.major,
                                       'V2', V1=1, R1=R1, R2=p[0], C2=p[1],
                                       f=freq) - y

    def fit_resistor_params(x):
        resistor_index = x['resistor index'].values[0]
        x['attenuation'] = x['board measured V'] / x['oscope measured V']
        p0 = [calibration.C_hv[resistor_index],
              calibration.R_hv[resistor_index]]

        p1, success = optimize.leastsq(e, p0,
                                       args=(x['frequency'],
                                             x['attenuation'], R1))
        return pd.DataFrame([p0 + p1.tolist()],
                            columns=['original C', 'original R',
                                     'fitted C', 'fitted R']).T

    results = (max_resistor_readings
               [max_resistor_readings['resistor index'] >= 0]
               .groupby(['resistor index']).apply(fit_resistor_params))
    data = results.unstack()
    data.columns = data.columns.droplevel()
    return data


def plot_feedback_params(hw_major_version, max_resistor_readings,
                         feedback_params, axis=None):
    '''
    Plot the effective attenuation _(i.e., gain less than 1)_ of the control
    board measurements of high-voltage AC input according to:

     - AC signal frequency.
     - feedback resistor used _(varies based on amplitude of AC signal)_.

    Each high-voltage feedback resistor (unintentionally) forms a low-pass
    filter, resulting in attenuation of the voltage measured on the control
    board.  The plot generated by this function plots each of the following
    trends for each feedback resistor:

     - Oscilloscope measurements.
     - Previous model of attenuation.
     - Newly fitted model of attenuation, based on oscilloscope readings.
    '''
    R1 = 10e6

    # Since the feedback circuit changed in version 2 of the control board, we
    # use the transfer function that corresponds to the current control board
    # version that the fitted attenuation model is based on.
    if axis is None:
        fig = plt.figure()
        axis = fig.add_subplot(111)
    colors = axis._get_lines.color_cycle
    markers = MarkerStyle.filled_markers

    def plot_resistor_params(args):
        resistor_index, x = args
        color = colors.next()

        F = feedback_params.loc[resistor_index]
        axis.loglog(x['frequency'],
                    compute_from_transfer_function(hw_major_version, 'V2',
                                                   V1=1., R1=R1,
                                                   R2=F['original R'],
                                                   C2=F['original C'],
                                                   f=x['frequency']),
                    color=color, linestyle='--',
                    label='R$_{%d}$ (previous fit)' % resistor_index)

        axis.loglog(x['frequency'],
                    compute_from_transfer_function(hw_major_version, 'V2',
                                                   V1=1., R1=R1,
                                                   R2=F['fitted R'],
                                                   C2=F['fitted C'],
                                                   f=x['frequency']),
                    color=color, linestyle='-', alpha=0.6,
                    label='R$_{%d}$ (new fit)' % resistor_index)
        attenuation = x['board measured V'] / x['oscope measured V']
        axis.plot(x['frequency'], attenuation, color='none',
                  marker=markers[resistor_index % len(markers)],
                  label='R$_{%d}$ (scope measured)' % resistor_index,
                  linestyle='none', markeredgecolor=color, markeredgewidth=2,
                  markersize=8)
        return 0

    map(plot_resistor_params, max_resistor_readings.groupby('resistor index'))
    legend = axis.legend(ncol=3)
    legend.draw_frame(False)
    axis.set_xlabel('Frequency (Hz)')
    axis.set_ylabel(r'$\frac{V_{BOARD}}'
                    r'{V_{SCOPE}}$', fontsize=25)


def update_control_board_calibration(control_board, fitted_params):
    '''
    Update the control board with the specified fitted parameters.
    '''
    def update_control_board_params(x):
        resistor_index = int(x.name)
        control_board.set_series_resistor_index(0, resistor_index)
        control_board.set_series_resistance(0, abs(x['fitted R']))
        control_board.set_series_capacitance(0, abs(x['fitted C']))

    # For each resistor index, update the control board with the new
    # fitted capacitor and resistor values.
    fitted_params[['fitted C', 'fitted R']].apply(
        update_control_board_params, axis=1)

    # Reconnnect to update calibration data.
    control_board.connect()
