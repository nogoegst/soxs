import numpy as np
import subprocess
import tempfile
import shutil
import os
from soxs.utils import soxs_files_path, mylog, \
    parse_prng, parse_value, line_width_equiv
from soxs.constants import erg_per_keV, hc, \
    sigma_to_fwhm, sqrt2pi
import astropy.units as u
import h5py
from scipy.interpolate import InterpolatedUnivariateSpline
from astropy.modeling.functional_models import \
    Gaussian1D
from soxs.apec import ApecGenerator


class Energies(u.Quantity):
    def __new__(cls, energy, flux):
        ret = u.Quantity.__new__(cls, energy, unit="keV")
        ret.flux = u.Quantity(flux, "erg/(cm**2*s)")
        return ret


def _generate_energies(spec, t_exp, rate, prng, quiet=False):
    cumspec = spec.cumspec
    n_ph = prng.poisson(t_exp*rate)
    if not quiet:
        mylog.info("Creating %d energies from this spectrum." % n_ph)
    randvec = prng.uniform(size=n_ph)
    randvec.sort()
    e = np.interp(randvec, cumspec, spec.ebins.value)
    if not quiet:
        mylog.info("Finished creating energies.")
    return e


class Spectrum:
    _units = "photon/(cm**2*s*keV)"

    def __init__(self, ebins, flux):
        self.ebins = u.Quantity(ebins, "keV")
        self.emid = 0.5*(self.ebins[1:]+self.ebins[:-1])
        self.flux = u.Quantity(flux, self._units)
        self.nbins = len(self.emid)
        self.de = np.diff(self.ebins)
        self._compute_total_flux()

    def _compute_total_flux(self):
        self.total_flux = (self.flux*self.de).sum()
        self.total_energy_flux = (self.flux*self.emid.to("erg")*self.de).sum()/(1.0*u.photon)
        cumspec = np.cumsum((self.flux*self.de).value)
        cumspec = np.insert(cumspec, 0, 0.0)
        cumspec /= cumspec[-1]
        self.cumspec = cumspec
        self.func = lambda e: np.interp(e, self.emid.value, self.flux.value)

    def _check_binning_units(self, other):
        if self.nbins != other.nbins or \
                not np.isclose(self.ebins.value, other.ebins.value).all():
            raise RuntimeError("Energy binning for these two "
                               "spectra is not the same!!")
        if self._units != other._units:
            raise RuntimeError("The units for these two spectra "
                               "are not the same!")

    def __add__(self, other):
        self._check_binning_units(other)
        return type(self)(self.ebins, self.flux+other.flux)

    def __iadd__(self, other):
        self._check_binning_units(other)
        self.flux += other.flux
        return self

    def __sub__(self, other):
        self._check_binning_units(other)
        return type(self)(self.ebins, self.flux-other.flux)

    def __mul__(self, other):
        if hasattr(other, "eff_area"):
            return ConvolvedSpectrum.convolve(self, other)
        else:
            return type(self)(self.ebins, other*self.flux)

    __rmul__ = __mul__

    def __truediv__(self, other):
        return type(self)(self.ebins, self.flux/other)

    __div__ = __truediv__

    def __repr__(self):
        s = f"{type(self).__name__} ({self.ebins[0]} - {self.ebins[-1]})\n"
        s += f"    Total Flux:\n    {self.total_flux}\n    {self.total_energy_flux}\n"
        return s

    def __call__(self, e):
        if hasattr(e, "to_astropy"):
            e = e.to_astropy()
        if isinstance(e, u.Quantity):
            e = e.to("keV").value
        return u.Quantity(self.func(e), self._units)

    def restrict_within_band(self, emin=None, emax=None):
        if emin is not None:
            emin = parse_value(emin, "keV")
            self.flux[self.emid.value < emin] = 0.0
        if emax is not None:
            emax = parse_value(emax, "keV")
            self.flux[self.emid.value > emax] = 0.0
        self._compute_total_flux()

    def get_flux_in_band(self, emin, emax):
        """
        Determine the total flux within a band specified 
        by an energy range. 

        Parameters
        ----------
        emin : float, (value, unit) tuple, or :class:`~astropy.units.Quantity`
            The minimum energy in the band, in keV.
        emax : float, (value, unit) tuple, or :class:`~astropy.units.Quantity`
            The maximum energy in the band, in keV.

        Returns
        -------
        A tuple of values for the flux/intensity in the 
        band: the first value is in terms of the photon 
        rate, the second value is in terms of the energy rate. 
        """
        emin = parse_value(emin, "keV")
        emax = parse_value(emax, "keV")
        range = np.logical_and(self.emid.value >= emin, self.emid.value <= emax)
        pflux = (self.flux*self.de)[range].sum()
        eflux = (self.flux*self.emid.to("erg")*self.de)[range].sum()/(1.0*u.photon)
        return pflux, eflux

    @classmethod
    def from_xspec_script(cls, infile, emin, emax, nbins):
        """
        Create a model spectrum using a script file as 
        input to XSPEC.

        Parameters
        ----------
        infile : string
            Path to the script file to use. 
        emin : float, (value, unit) tuple, or :class:`~astropy.units.Quantity`
            The minimum energy of the spectrum in keV. 
        emax : float, (value, unit) tuple, or :class:`~astropy.units.Quantity`
            The maximum energy of the spectrum in keV. 
        nbins : integer
            The number of bins in the spectrum.
        """
        with open(infile, "r") as f:
            xspec_in = f.readlines()
        return cls._from_xspec(xspec_in, emin, emax, nbins)

    @classmethod
    def from_xspec_model(cls, model_string, params, emin, emax, nbins):
        """
        Create a model spectrum using a model string and parameters
        as input to XSPEC.

        Parameters
        ----------
        model_string : string
            The model to create the spectrum from. Use standard XSPEC
            model syntax. Example: "wabs*mekal"
        params : list
            The list of parameters for the model. Must be in the order
            that XSPEC expects.
        emin : float, (value, unit) tuple, or :class:`~astropy.units.Quantity`
            The minimum energy of the spectrum in keV
        emax : float, (value, unit) tuple, or :class:`~astropy.units.Quantity`
            The maximum energy of the spectrum in keV
        nbins : integer
            The number of bins in the spectrum.
        """
        xspec_in = []
        model_str = "%s &" % model_string
        for param in params:
            model_str += " %g &" % param
        model_str += " /*"
        xspec_in.append("model %s\n" % model_str)
        return cls._from_xspec(xspec_in, emin, emax, nbins)

    @classmethod
    def _from_xspec(cls, xspec_in, emin, emax, nbins):
        emin = parse_value(emin, "keV")
        emax = parse_value(emax, "keV")
        tmpdir = tempfile.mkdtemp()
        curdir = os.getcwd()
        os.chdir(tmpdir)
        xspec_in.append("dummyrsp %g %g %d lin\n" % (emin, emax, nbins))
        xspec_in += ["set fp [open spec_therm.xspec w+]\n",
                     "tclout energies\n", "puts $fp $xspec_tclout\n",
                     "tclout modval\n", "puts $fp $xspec_tclout\n",
                     "close $fp\n", "quit\n"]
        with open("xspec.in", "w") as f_xin:
            f_xin.writelines(xspec_in)
        logfile = os.path.join(curdir, "xspec.log")
        with open(logfile, "ab") as xsout:
            subprocess.call(["xspec", "-", "xspec.in"],
                            stdout=xsout, stderr=xsout)
        with open("spec_therm.xspec", "r") as f_s:
            lines = f_s.readlines()
        ebins = np.array(lines[0].split()).astype("float64")
        de = np.diff(ebins)
        flux = np.array(lines[1].split()).astype("float64")/de
        os.chdir(curdir)
        shutil.rmtree(tmpdir)
        return cls(ebins, flux)

    @classmethod
    def from_powerlaw(cls, photon_index, redshift, norm, emin, emax,
                      nbins):
        """
        Create a spectrum from a power-law model.

        Parameters
        ----------
        photon_index : float
            The photon index of the source.
        redshift : float
            The redshift of the source.
        norm : float
            The normalization of the source in units of
            photons/s/cm**2/keV at 1 keV in the source 
            frame.
        emin : float, (value, unit) tuple, or :class:`~astropy.units.Quantity`
            The minimum energy of the spectrum in keV. 
        emax : float, (value, unit) tuple, or :class:`~astropy.units.Quantity`
            The maximum energy of the spectrum in keV. 
        nbins : integer
            The number of bins in the spectrum. 
        """
        emin = parse_value(emin, 'keV')
        emax = parse_value(emax, 'keV')
        ebins = np.linspace(emin, emax, nbins+1)
        emid = 0.5*(ebins[1:]+ebins[:-1])
        flux = norm*(emid*(1.0+redshift))**(-photon_index)
        return cls(ebins, flux)

    @classmethod
    def from_file(cls, filename):
        """
        Read a spectrum from an ASCII or HDF5 file.

        If ASCII: accepts a file with two columns,
        the first being the center energy of the bin in 
        keV and the second being the spectrum in the
        appropriate units, assuming a linear binning 
        with constant bin widths.

        If HDF5: accepts a file with one array dataset, 
        named "spectrum", which is the spectrum in the 
        appropriate units, and two scalar datasets, 
        "emin" and "emax", which are the minimum and 
        maximum energies in keV.

        Parameters
        ----------
        filename : string
            The path to the file containing the spectrum.
        """
        arf = None
        if filename.endswith(".h5"):
            with h5py.File(filename, "r") as f:
                flux = f["spectrum"][()]
                nbins = flux.size
                ebins = np.linspace(f["emin"][()], f["emax"][()], nbins+1)
                if "arf" in f.attrs:
                    arf = f.attrs["arf"]
        else:
            emid, flux = np.loadtxt(filename, unpack=True)
            de = np.diff(emid)[0]
            ebins = np.append(emid-0.5*de, emid[-1]+0.5*de)
        if arf is not None:
            return cls(ebins, flux, arf)
        else:
            return cls(ebins, flux)

    @classmethod
    def from_constant(cls, const_flux, emin, emax, nbins):
        """
        Create a spectrum from a constant model using 
        XSPEC.

        Parameters
        ----------
        const_flux : float
            The value of the constant flux in the units 
            of the spectrum. 
        emin : float, (value, unit) tuple, or :class:`~astropy.units.Quantity`
            The minimum energy of the spectrum in keV. 
        emax : float, (value, unit) tuple, or :class:`~astropy.units.Quantity`
            The maximum energy of the spectrum in keV. 
        nbins : integer
            The number of bins in the spectrum.
        """
        emin = parse_value(emin, "keV")
        emax = parse_value(emax, 'keV')
        ebins = np.linspace(emin, emax, nbins+1)
        flux = const_flux*np.ones(nbins)
        return cls(ebins, flux)

    def _new_spec_from_band(self, emin, emax):
        emin = parse_value(emin, "keV")
        emax = parse_value(emax, 'keV')
        band = np.logical_and(self.ebins.value >= emin,
                              self.ebins.value <= emax)
        idxs = np.where(band)[0]
        ebins = self.ebins.value[idxs]
        flux = self.flux.value[idxs[:-1]]
        return ebins, flux

    def new_spec_from_band(self, emin, emax):
        """
        Create a new :class:`~soxs.spectra.Spectrum` object
        from a subset of an existing one defined by a particular
        energy band.

        Parameters
        ----------
        emin : float, (value, unit) tuple, or :class:`~astropy.units.Quantity`
            The minimum energy of the band in keV.
        emax : float, (value, unit) tuple, or :class:`~astropy.units.Quantity`
            The maximum energy of the band in keV.
        """
        ebins, flux = self._new_spec_from_band(emin, emax)
        return Spectrum(ebins, flux)

    def rescale_flux(self, new_flux, emin=None, emax=None, flux_type="photons"):
        """
        Rescale the flux of the spectrum, optionally using 
        a specific energy band.

        Parameters
        ----------
        new_flux : float
            The new flux in units of photons/s/cm**2.
        emin : float, (value, unit) tuple, or :class:`~astropy.units.Quantity`, optional
            The minimum energy of the band to consider, 
            in keV. Default: Use the minimum energy of 
            the entire spectrum.
        emax : float, (value, unit) tuple, or :class:`~astropy.units.Quantity`, optional
            The maximum energy of the band to consider, 
            in keV. Default: Use the maximum energy of 
            the entire spectrum.
        flux_type : string, optional
            The units of the flux to use in the rescaling:
                "photons": photons/s/cm**2
                "energy": erg/s/cm**2
        """
        if emin is None:
            emin = self.ebins[0].value
        if emax is None:
            emax = self.ebins[-1].value
        emin = parse_value(emin, "keV")
        emax = parse_value(emax, 'keV')
        idxs = np.logical_and(self.emid.value >= emin, self.emid.value <= emax)
        if flux_type == "photons":
            f = (self.flux*self.de)[idxs].sum()
        elif flux_type == "energy":
            f = (self.flux*self.emid.to("erg")*self.de)[idxs].sum()
        self.flux *= new_flux/f.value
        self._compute_total_flux()

    def write_file(self, specfile, overwrite=False):
        """
        Write the spectrum to a file.

        Parameters
        ----------
        specfile : string
            The filename to write the file to.
        overwrite : boolean, optional
            Whether or not to overwrite an existing 
            file with the same name. Default: False
        """
        if os.path.exists(specfile) and not overwrite:
            raise IOError(f"File {specfile} exists and overwrite=False!")
        header = f"Energy\tFlux\nkeV\t{self._units}"
        np.savetxt(specfile, np.transpose([self.emid, self.flux]), 
                   delimiter="\t", header=header)

    def write_h5_file(self, specfile, overwrite=False):
        """
        Write the spectrum to an HDF5 file.

        Parameters
        ----------
        specfile : string
            The filename to write the file to.
        overwrite : boolean, optional
            Whether or not to overwrite an existing 
            file with the same name. Default: False
        """
        if os.path.exists(specfile) and not overwrite:
            raise IOError("File %s exists and overwrite=False!" % specfile)
        with h5py.File(specfile, "w") as f:
            f.create_dataset("emin", data=self.ebins[0].value)
            f.create_dataset("emax", data=self.ebins[-1].value)
            f.create_dataset("spectrum", data=self.flux.value)
            if hasattr(self, "arf"):
                f.attrs["arf"] = self.arf.filename

    def apply_foreground_absorption(self, nH, model="wabs", redshift=0.0):
        """
        Given a hydrogen column density, apply
        galactic foreground absorption to the spectrum.

        Parameters
        ----------
        nH : float, (value, unit) tuple, or :class:`~astropy.units.Quantity`
            The hydrogen column in units of 10**22 atoms/cm**2
        model : string, optional
            The model for absorption to use. Options are "wabs"
            (Wisconsin, Morrison and McCammon; ApJ 270, 119) or
            "tbabs" (Tuebingen-Boulder, Wilms, J., Allen, A., & 
            McCray, R. 2000, ApJ, 542, 914). Default: "wabs".
        redshift : float, optional
            The redshift of the absorbing material. Default: 0.0
        """
        nH = parse_value(nH, "1.0e22*cm**-2")
        e = self.emid.value*(1.0+redshift)
        if model == "wabs":
            sigma = wabs_cross_section(e)
        elif model == "tbabs":
            sigma = tbabs_cross_section(e)
        self.flux *= np.exp(-nH*1.0e22*sigma)
        self._compute_total_flux()

    def add_emission_line(self, line_center, line_width, line_amp,
                          line_type="gaussian"):
        """
        Add an emission line to this spectrum.

        Parameters
        ----------
        line_center : float, (value, unit) tuple, or :class:`~astropy.units.Quantity`
            The line center position in units of keV, in the observer frame.
        line_width : one or more float, (value, unit) tuple, or :class:`~astropy.units.Quantity`
            The line width (FWHM) in units of keV, in the observer frame. Can also
            input the line width in units of velocity in the rest frame. For the Voigt
            profile, a list, tuple, or array of two values should be provided since there
            are two line widths, the Lorentzian and the Gaussian (in that order).
        line_amp : float, (value, unit) tuple, or :class:`~astropy.units.Quantity`
            The integrated line amplitude in the units of the flux 
        line_type : string, optional
            The line profile type. Default: "gaussian"
        """
        line_center = parse_value(line_center, "keV")
        line_width = parse_value(line_width, "keV", equivalence=line_width_equiv(line_center))
        line_amp = parse_value(line_amp, self._units)
        if line_type == "gaussian":
            sigma = line_width / sigma_to_fwhm
            line_amp /= sqrt2pi * sigma
            f = Gaussian1D(line_amp, line_center, sigma)
        else:
            raise NotImplementedError("Line profile type '%s' " % line_type +
                                      "not implemented!")
        self.flux += u.Quantity(f(self.emid.value), self._units)
        self._compute_total_flux()

    def add_absorption_line(self, line_center, line_width, equiv_width, 
                            line_type='gaussian'):
        """
        Add an absorption line to this spectrum.

        Parameters
        ----------
        line_center : float, (value, unit) tuple, or :class:`~astropy.units.Quantity`
            The line center position in units of keV, in the observer frame.
        line_width : one or more float, (value, unit) tuple, or :class:`~astropy.units.Quantity`
            The line width (FWHM) in units of keV, in the observer frame. Can also
            input the line width in units of velocity in the rest frame. For the Voigt
            profile, a list, tuple, or array of two values should be provided since there
            are two line widths, the Lorentzian and the Gaussian (in that order).
        equiv_width : float, (value, unit) tuple, or :class:`~astropy.units.Quantity`
            The equivalent width of the line, in units of milli-Angstrom
        line_type : string, optional
            The line profile type. Default: "gaussian"
        """
        line_center = parse_value(line_center, "keV")
        line_width = parse_value(line_width, "keV", equivalence=line_width_equiv(line_center))
        equiv_width = parse_value(equiv_width, "1.0e-3*angstrom") # in milliangstroms
        equiv_width *= 1.0e-3 # convert to angstroms
        if line_type == "gaussian":
            sigma = line_width / sigma_to_fwhm
            B = equiv_width*line_center*line_center
            B /= hc * sqrt2pi * sigma
            f = Gaussian1D(B, line_center, sigma)
        else:
            raise NotImplementedError("Line profile type '%s' " % line_type +
                                      "not implemented!")
        self.flux *= np.exp(-f(self.emid.value))
        self._compute_total_flux()

    def generate_energies(self, t_exp, area, prng=None, quiet=False):
        """
        Generate photon energies from this spectrum 
        given an exposure time and effective area.

        Parameters
        ----------
        t_exp : float, (value, unit) tuple, or :class:`~astropy.units.Quantity`
            The exposure time in seconds.
        area : float, (value, unit) tuple, or :class:`~astropy.units.Quantity`
            The effective area in cm**2. If one is creating 
            events for a SIMPUT file, a constant should be 
            used and it must be large enough so that a 
            sufficiently large sample is drawn for the ARF.
        prng : :class:`~numpy.random.RandomState` object, integer, or None
            A pseudo-random number generator. Typically will only 
            be specified if you have a reason to generate the same 
            set of random numbers, such as for a test. Default is None, 
            which sets the seed based on the system time. 
        quiet : boolean, optional
            If True, log messages will not be displayed when 
            creating energies. Useful if you have to loop over 
            a lot of spectra. Default: False
        """
        t_exp = parse_value(t_exp, "s")
        area = parse_value(area, "cm**2")
        prng = parse_prng(prng)
        rate = area*self.total_flux.value
        energy = _generate_energies(self, t_exp, rate, prng, quiet=quiet)
        flux = np.sum(energy)*erg_per_keV/t_exp/area
        energies = Energies(energy, flux)
        return energies

    def plot(self, lw=2, xmin=None, xmax=None, ymin=None, ymax=None,
             xscale=None, yscale=None, label=None, fontsize=18, 
             fig=None, ax=None, **kwargs):
        """
        Make a quick Matplotlib plot of the spectrum. A Matplotlib
        figure and axis is returned.

        Parameters
        ----------
        lw : float, optional
            The width of the lines in the plots. Default: 2.0 px.
        xmin : float, optional
            The left-most energy in keV to plot. Default is the 
            minimum value in the spectrum. 
        xmax : float, optional
            The right-most energy in keV to plot. Default is the 
            maximum value in the spectrum. 
        ymin : float, optional
            The lower extent of the y-axis. By default it is set automatically.
        ymax : float, optional
            The upper extent of the y-axis. By default it is set automatically.
        xscale : string, optional
            The scaling of the x-axis of the plot. Default: "log"
        yscale : string, optional
            The scaling of the y-axis of the plot. Default: "log"
        label : string, optional
            The label of the spectrum. Default: None
        fontsize : int
            Font size for labels and axes. Default: 18
        fig : :class:`~matplotlib.figure.Figure`, optional
            A Figure instance to plot in. Default: None, one will be
            created if not provided.
        ax : :class:`~matplotlib.axes.Axes`, optional
            An Axes instance to plot in. Default: None, one will be
            created if not provided.

        Returns
        -------
        A tuple of the :class:`~matplotlib.figure.Figure` and the :class:`~matplotlib.axes.Axes` objects.
        """
        import matplotlib.pyplot as plt
        if fig is None:
            fig = plt.figure(figsize=(10, 10))
        if xscale is None:
            if ax is None:
                xscale = "log"
            else:
                xscale = ax.get_xscale()
        if yscale is None:
            if ax is None:
                yscale = "log"
            else:
                yscale = ax.get_yscale()
        if ax is None:
            ax = fig.add_subplot(111)
        ax.plot(self.emid, self.flux, lw=lw, label=label, **kwargs)
        ax.set_xscale(xscale)
        ax.set_yscale(yscale)
        ax.set_xlim(xmin, xmax)
        ax.set_ylim(ymin, ymax)
        ax.set_xlabel("Energy (keV)", fontsize=fontsize)
        yunit = u.Unit(self._units).to_string("latex").replace("{}^{\\prime}", "arcmin")
        ax.set_ylabel("Spectrum (%s)" % yunit, fontsize=fontsize)
        ax.tick_params(axis='both',labelsize=fontsize)
        return fig, ax


def wabs_cross_section(E):
    emax = np.array([0.0, 0.1, 0.284, 0.4, 0.532, 0.707, 0.867, 1.303, 1.840, 
                     2.471, 3.210, 4.038, 7.111, 8.331, 10.0])
    c0 = np.array([17.3, 34.6, 78.1, 71.4, 95.5, 308.9, 120.6, 141.3,
                   202.7,342.7,352.2,433.9,629.0,701.2])
    c1 = np.array([608.1, 267.9, 18.8, 66.8, 145.8, -380.6, 169.3,
                   146.8, 104.7, 18.7, 18.7, -2.4, 30.9, 25.2]) 
    c2 = np.array([-2150., -476.1 ,4.3, -51.4, -61.1, 294.0, -47.7,
                   -31.5, -17.0, 0.0, 0.0, 0.75, 0.0, 0.0])
    idxs = np.minimum(np.searchsorted(emax, E)-1, 13)
    sigma = (c0[idxs]+c1[idxs]*E+c2[idxs]*E*E)*1.0e-24/E**3
    return sigma


def get_wabs_absorb(e, nH):
    sigma = wabs_cross_section(e)
    return np.exp(-nH*1.0e22*sigma)


_tbabs_emid = None
_tbabs_sigma = None
_tbabs_spline = None


def tbabs_cross_section(E):
    global _tbabs_emid
    global _tbabs_sigma
    global _tbabs_spline
    if _tbabs_spline is None:
        filename = os.path.join(soxs_files_path, "tbabs_table.h5")
        f = h5py.File(filename, "r")
        _tbabs_sigma = f["cross_section"][:]
        nbins = _tbabs_sigma.size
        ebins = np.linspace(f["emin"][()], f["emax"][()], nbins+1)
        f.close()
        _tbabs_emid = 0.5*(ebins[1:]+ebins[:-1])
        _tbabs_spline = InterpolatedUnivariateSpline(_tbabs_emid,
                                                     _tbabs_sigma, k=5, 
                                                     ext=1)
    return _tbabs_spline(E)


def get_tbabs_absorb(e, nH):
    sigma = tbabs_cross_section(e)
    return np.exp(-nH*1.0e22*sigma)


class CountRateSpectrum(Spectrum):
    _units = "photon/(s*keV)"

    def generate_energies(self, t_exp, prng=None, quiet=False):
        """
        Generate photon energies from this count rate spectrum given an
        exposure time.

        Parameters
        ----------
        t_exp : float, (value, unit) tuple, or :class:`~astropy.units.Quantity`
            The exposure time in seconds.
        prng : :class:`~numpy.random.RandomState` object, integer, or None
            A pseudo-random number generator. Typically will only
            be specified if you have a reason to generate the same 
            set of random numbers, such as for a test. Default is None,
            which sets the seed based on the system time.
        quiet : boolean, optional
            If True, log messages will not be displayed when 
            creating energies. Useful if you have to loop over 
            a lot of spectra. Default: False
        """
        t_exp = parse_value(t_exp, "s")
        prng = parse_prng(prng)
        rate = self.total_flux.value
        energy = _generate_energies(self, t_exp, rate, prng, quiet=quiet)
        energies = u.Quantity(energy, "keV")
        return energies

    @classmethod
    def from_xspec_model(cls, model_string, params, emin=0.01, emax=50.0,
                         nbins=10000):
        raise NotImplementedError

    @classmethod
    def from_xspec_script(cls, infile, emin=0.01, emax=50.0, nbins=10000):
        raise NotImplementedError


class ConvolvedSpectrum(CountRateSpectrum):

    def __init__(self, ebins, flux, arf):
        from numbers import Number
        from soxs.response import AuxiliaryResponseFile, FlatResponse
        super(ConvolvedSpectrum, self).__init__(ebins, flux)
        if isinstance(arf, Number):
            arf = FlatResponse(ebins[0], ebins[-1], arf, ebins.size-1)
        elif isinstance(arf, str):
            arf = AuxiliaryResponseFile(arf)
        self.arf = arf

    def __add__(self, other):
        self._check_binning_units(other)
        return ConvolvedSpectrum(self.ebins, self.flux+other.flux, self.arf)

    def __sub__(self, other):
        self._check_binning_units(other)
        return ConvolvedSpectrum(self.ebins, self.flux-other.flux, self.arf)

    @classmethod
    def convolve(cls, spectrum, arf):
        """
        Generate a convolved spectrum by convolving a spectrum with an
        ARF.

        Parameters
        ----------
        spectrum : :class:`~soxs.spectra.Spectrum` object
            The input spectrum to convolve with.
        arf : string or :class:`~soxs.instrument.AuxiliaryResponseFile`
            The ARF to use in the convolution.
        """
        from soxs.response import AuxiliaryResponseFile
        if not isinstance(arf, AuxiliaryResponseFile):
            arf = AuxiliaryResponseFile(arf)
        earea = arf.interpolate_area(spectrum.emid.value)
        rate = spectrum.flux * earea
        return cls(spectrum.ebins, rate, arf)

    def new_spec_from_band(self, emin, emax):
        """
        Create a new :class:`~soxs.spectra.ConvolvedSpectrum` object
        from a subset of an existing one defined by a particular
        energy band.

        Parameters
        ----------
        emin : float, (value, unit) tuple, or :class:`~astropy.units.Quantity`
            The minimum energy of the band in keV.
        emax : float, (value, unit) tuple, or :class:`~astropy.units.Quantity`
            The maximum energy of the band in keV.
        """
        ebins, flux = self._new_spec_from_band(emin, emax)
        return ConvolvedSpectrum(ebins, flux, self.arf)

    def deconvolve(self):
        """
        Return the deconvolved :class:`~soxs.spectra.Spectrum`
        object associated with this convolved spectrum.
        """
        earea = self.arf.interpolate_area(self.emid)
        flux = self.flux / earea
        flux = np.nan_to_num(flux.value)
        return Spectrum(self.ebins.value, flux)

    def generate_energies(self, t_exp, prng=None, quiet=False):
        """
        Generate photon energies from this convolved spectrum given an
        exposure time.

        Parameters
        ----------
        t_exp : float, (value, unit) tuple, or :class:`~astropy.units.Quantity`
            The exposure time in seconds.
        prng : :class:`~numpy.random.RandomState` object, integer, or None
            A pseudo-random number generator. Typically will only 
            be specified if you have a reason to generate the same 
            set of random numbers, such as for a test. Default is None, 
            which sets the seed based on the system time.
        quiet : boolean, optional
            If True, log messages will not be displayed when 
            creating energies. Useful if you have to loop over 
            a lot of spectra. Default: False
        """
        t_exp = parse_value(t_exp, "s")
        prng = parse_prng(prng)
        rate = self.total_flux.value
        energy = _generate_energies(self, t_exp, rate, prng, quiet=quiet)
        earea = self.arf.interpolate_area(energy).value
        flux = np.sum(energy)*erg_per_keV/t_exp/earea.sum()
        energies = Energies(energy, flux)
        return energies

    def apply_foreground_absorption(self, nH, model="wabs"):
        raise NotImplementedError

    @classmethod
    def from_constant(cls, const_flux, emin=0.01, emax=50.0, nbins=10000):
        raise NotImplementedError

    @classmethod
    def from_powerlaw(cls, photon_index, redshift, norm,
                      emin=0.01, emax=50.0, nbins=10000):
        raise NotImplementedError

    @classmethod
    def from_xspec_model(cls, model_string, params, emin=0.01, emax=50.0,
                         nbins=10000):
        raise NotImplementedError

    @classmethod
    def from_xspec_script(cls, infile, emin=0.01, emax=50.0, nbins=10000):
        raise NotImplementedError
