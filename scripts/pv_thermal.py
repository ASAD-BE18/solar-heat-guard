"""PV cell-temperature and power-loss model.

Standard NOCT (Nominal Operating Cell Temperature) model, the basis of
almost every PV module datasheet's thermal rating:

    T_cell = T_ambient + (NOCT - 20) * (G / 800)

where G is plane-of-array irradiance (W/m^2); 20 C and 800 W/m^2 are the NOCT
test reference conditions.

Power loss from a linear temperature coefficient (also a datasheet spec):

    loss_pct = temp_coeff_pct_per_c * (T_cell - 25)

temp_coeff_pct_per_c is negative for standard c-Si modules, so loss_pct comes
out negative (a loss) once T_cell rises above the 25 C STC reference.

Defaults below (NOCT=44 C, temp_coeff=-0.38 %/C, max_operating_temp=85 C) are
typical values for a standard utility-scale c-Si module, not a real spec for
this site's actual panels -- override with datasheet numbers if available.
"""

NOCT_DEFAULT_C = 44.0
NOCT_REF_AMBIENT_C = 20.0
NOCT_REF_IRRADIANCE_W_M2 = 800.0
TEMP_COEFF_DEFAULT_PCT_PER_C = -0.38
POWER_REF_TEMP_C = 25.0
MAX_OPERATING_TEMP_DEFAULT_C = 85.0


def cell_temperature(ambient_c, irradiance_w_m2, noct_c=NOCT_DEFAULT_C):
    return ambient_c + (noct_c - NOCT_REF_AMBIENT_C) * (irradiance_w_m2 / NOCT_REF_IRRADIANCE_W_M2)


def power_loss_percent(cell_temp_c, temp_coeff_pct_per_c=TEMP_COEFF_DEFAULT_PCT_PER_C):
    return temp_coeff_pct_per_c * (cell_temp_c - POWER_REF_TEMP_C)
