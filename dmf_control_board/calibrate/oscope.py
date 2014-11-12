import warnings
try:
    import visa
    import time
    VISA_AVAILABLE = True
except ImportError:
    warnings.warn('Could not import `visa`.')
    VISA_AVAILABLE = False


class AgilentOscope(object):
    def __init__(self, address=None):
        self.rm = visa.ResourceManager()
        if address is None:
            address = self.rm.list_resources()[0]
        self.device = self.rm.get_instrument(address)

    def read_ac_vrms(self):
        time.sleep(.75)
        self.device.write("AUTOSCALE")
        time.sleep(.75)
        self.device.write("MEASURE:VRMS? DISPLAY,AC")
        time.sleep(.75)
        return float(self.device.read())


# `VisaIOError`:
#  - Oscilloscope unplugged while running without restarting.
#  - Oscilloscope not plugged in after reboot.


def get_oscope_reading():
    import PyZenity

    response = None
    while response is None:
        response = PyZenity.GetText()
    return float(response)
