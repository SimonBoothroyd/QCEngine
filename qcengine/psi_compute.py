"""
Calls the Psi4 executable.
"""

import time
import sys
from pkg_resources import parse_version

from . import config


def _build_v11_mol(jm):
    """
    Converts a QC JSON molecule to the Psi4 expected string for Psi4 version 1.1.
    """

    # Cannot handle fragments
    if "fragments" in jm:
        return (False, "Psi4 v1.1 cannot support fragments")

    if "real" in jm:
        real = jm["real"]
    else:
        real = [True for x in range(len(jm["symbols"]))]

    # Charge and multiplicity, skip otherwise
    psimol = ""
    if ("molecular_charge" in jm) or ("molecular_multiplicity" in jm):
        psimol += "\n    "
        if "molecular_charge" in jm:
            psimol += "%d  " % jm["molecular_charge"]
        else:
            psimol += "0 "

        if "molecular_multiplicity" in jm:
            psimol += "%d" % jm["molecular_multiplicity"]
        else:
            psimol += "1"

    psimol += "\n"

    for x in range(len(jm["symbols"])):
        shift = x * 3
        if real[x]:
            psimol += "   %-5s" % jm["symbols"][x]
        else:
            psimol += "Gh(%-5s)" % jm["symbols"][x]

        psimol += "    % 14.10f % 14.10f % 14.10f\n" % (jm["geometry"][shift], jm["geometry"][shift + 1],
                                                        jm["geometry"][shift + 2])

    psimol += "   units bohr"
    if "fix_com" in jm and jm["fix_com"]:
        psimol += "\n   no_com"

    if "fix_orientation" in jm and jm["fix_orientation"]:
        psimol += "\n   no_reorient"

    return psimol

def _parse_psi_version(version):
    if "undef" in version:
        raise TypeError("Using custom build Psi4 without tags. Please `git pull origin master --tags` and recompile Psi4.")

    return parse_version(version)



def run_psi4(json):
    """
    Runs Psi4 in API mode
    """

    # Insert API path if needed
    psiapi = config.get_config("psi_path")
    if (psiapi is not None) and (psiapi not in sys.path):
        sys.path.insert(1, psiapi)

    try:
        import psi4
    except ImportError:
        raise ImportError("Could not find Psi4 in the Python path.")

    # Setup the job
    psi4.set_num_threads(config.get_config("cores_per_job"))
    json["memory"] = int(config.get_config("memory_per_job") * 1024 * 1024 * 1024 * 0.9)
    json["success"] = False

    scratch = config.get_config("scratch_directory")
    if scratch is not None:
        json["scratch_location"] = scratch

    psi_version = _parse_psi_version(psi4.__version__)

    if psi_version == parse_version("1.1"):

        json_mol = json["molecule"]
        mol_str = _build_v11_mol(json_mol)

        json["options"] = json["keywords"]
        json["options"]["BASIS"] = json["model"]["basis"]

        # Check if RHF/UHF
        mol = psi4.geometry(mol_str)
        wfn = psi4.core.Wavefunction.build(mol, "def2-SVP")
        if wfn.molecule().multiplicity() != 1:
            json["options"]["reference"] = "uks"

        json["args"] = (json["model"]["method"], )

        # v1.1 wanted an actual string
        json["molecule"] = mol_str

        # Compute!
        rjson = psi4.json_wrapper.run_json(json)
        psi4.core.clean()
        if rjson is False:
            json["success"] = False
            if "error" not in json:
                json["error"] = "Unspecified error occured."

        json["molecule"] = json_mol

    elif psi_version > parse_version("1.2rc2.dev500"):
        mol = psi4.core.Molecule.from_schema(json)
        if mol.multiplicity() != 1:
            json["keywords"]["reference"] = "uks"

        rjson = psi4.json_wrapper.run_json(json)


    else:
        raise TypeError("Psi4 version '{}' not understood".format(psi_version))

    # Dispatch errors, PSIO Errors are not recoverable for future runs
    if json["success"] is False:

        if "PSIO Error" in json["error"]:
            raise ValueError(json["error"])

    return json
