from collections import defaultdict
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple, Union

import numpy as np
from pydantic import Field, validator
from qcelemental.models import ComputeError, DriverEnum, FailedOperation, Molecule, ProtoModel, Provenance
from qcelemental.models.procedures import (
    OptimizationInput,
    OptimizationProtocols,
    OptimizationResult,
    QCInputSpecification,
)
from qcelemental.util import which_import

from ..extras import provenance_stamp
from .model import ProcedureHarness

if TYPE_CHECKING:
    from ..config import TaskConfig


class OptimizationSpecification(ProtoModel):

    procedure: str = Field(..., description="Optimization procedure to run the optimization with.")
    keywords: Dict[str, Any] = Field({}, description="The optimization specific keywords to be used.")
    protocols: OptimizationProtocols = Field(OptimizationProtocols(), description=str(OptimizationProtocols.__doc__))

    @validator("procedure")
    def _check_procedure(cls, v):
        return v.lower()


class TorsionDriveInput(ProtoModel):

    keywords: Dict[str, Any] = Field({}, description="The torsion drive specific keywords to be used.")
    extras: Dict[str, Any] = Field({}, description="Extra fields that are not part of the schema.")

    input_specification: QCInputSpecification = Field(..., description=str(QCInputSpecification.__doc__))
    initial_molecule: Molecule = Field(..., description="The starting molecule for the torsion drive.")

    optimization_spec: OptimizationSpecification = Field(
        ..., description="Settings to use for optimizations at each grid angle."
    )

    provenance: Provenance = Field(Provenance(**provenance_stamp(__name__)), description=str(Provenance.__doc__))

    @validator("input_specification")
    def _check_input_specification(cls, value):
        assert value.driver == DriverEnum.gradient, "driver must be set to gradient"
        return value


class TorsionDriveResult(TorsionDriveInput):

    final_energies: Dict[str, float] = Field(
        ..., description="The final energy at each angle of the TorsionDrive scan."
    )
    final_molecules: Dict[str, Molecule] = Field(
        ..., description="The final molecule at each angle of the TorsionDrive scan."
    )

    optimization_history: Dict[str, List[OptimizationResult]] = Field(
        ...,
        description="The map of each angle of the TorsionDrive scan to each optimization computations.",
    )

    stdout: Optional[str] = Field(None, description="The standard output of the program.")
    stderr: Optional[str] = Field(None, description="The standard error of the program.")

    success: bool = Field(
        ..., description="The success of a given programs execution. If False, other fields may be blank."
    )
    error: Optional[ComputeError] = Field(None, description=str(ComputeError.__doc__))
    provenance: Provenance = Field(..., description=str(Provenance.__doc__))


class TorsionDriveProcedure(ProcedureHarness):

    _defaults = {"name": "TorsionDrive", "procedure": "torsiondrive"}

    class Config(ProcedureHarness.Config):
        pass

    def found(self, raise_error: bool = False) -> bool:
        return which_import(
            "torsiondrive",
            return_bool=True,
            raise_error=raise_error,
            raise_msg="Please install via `conda install torsiondrive -c conda-forge`.",
        )

    def build_input_model(self, data: Union[Dict[str, Any], "TorsionDriveInput"]) -> "TorsionDriveInput":
        return self._build_model(data, TorsionDriveInput)

    def compute(self, input_model: "TorsionDriveInput", config: "TaskConfig") -> "TorsionDriveResult":
        try:
            import torsiondrive
            from torsiondrive import td_api
        except ModuleNotFoundError:
            raise ModuleNotFoundError("Could not find TorsionDrive in the Python path.")

        dihedrals = input_model.keywords["dihedrals"]
        grid_spacing = input_model.keywords["grid_spacing"]

        dihedral_ranges = input_model.keywords.get("dihedral_ranges", None)

        energy_decrease_thresh = input_model.keywords.get("energy_decrease_thresh", None)
        energy_upper_limit = input_model.keywords.get("energy_upper_limit", None)

        state = td_api.create_initial_state(
            dihedrals=dihedrals,
            grid_spacing=grid_spacing,
            elements=input_model.initial_molecule.symbols,
            init_coords=[input_model.initial_molecule.geometry.flatten().tolist()],
            dihedral_ranges=dihedral_ranges,
            energy_upper_limit=energy_upper_limit,
            energy_decrease_thresh=energy_decrease_thresh,
        )

        optimization_results = defaultdict(list)
        error = None

        # Spawn new optimizations at each grid points until convergence / an error.
        while True:

            next_jobs = td_api.next_jobs_from_state(state, verbose=True)

            if len(next_jobs) == 0:
                break

            # TODO: Is it better to spawn multiple TDs at once each using one core
            #       or one TD but give the optimization multiple cores?
            grid_point_results = {
                grid_point: [self._spawn_optimization(grid_point, job, input_model, config) for job in jobs]
                for grid_point, jobs in next_jobs.items()
            }

            for grid_point, results in grid_point_results.items():

                failed_results = [result for result in results if not result.success]

                if len(failed_results) > 0:

                    error_message = failed_results[0].error.error_message
                    error = {
                        "error_type": "unknown",
                        "error_message": f"TorsionDrive error at {grid_point}:\n{error_message}",
                    }
                    break

                optimization_results[grid_point].extend(results)

            if error is not None:
                break

            task_results = {
                grid_point: [
                    (
                        result.initial_molecule.geometry.flatten().tolist(),
                        result.final_molecule.geometry.flatten().tolist(),
                        result.energies[-1],
                    )
                    for result in results
                ]
                for grid_point, results in grid_point_results.items()
            }

            td_api.update_state(state, {**task_results})

        output_data = input_model.dict()
        output_data["provenance"] = {
            "creator": "TorsionDrive",
            "routine": "torsiondrive.td_api.next_jobs_from_state",
            "version": torsiondrive.__version__,
        }
        output_data["success"] = error is None

        if error is None:

            output_data["final_energies"], output_data["final_molecules"] = {}, {}

            for grid_point, results in optimization_results.items():

                final_energy, final_molecule = self._find_final_results(results)

                output_data["final_energies"][grid_point] = final_energy
                output_data["final_molecules"][grid_point] = final_molecule

            output_data["optimization_history"] = optimization_results
            output_data = TorsionDriveResult(**output_data)

        else:
            output_data["error"] = error

        return output_data

    @staticmethod
    def _spawn_optimization(
        grid_point: str, job: List[float], input_model: "TorsionDriveInput", config: "TaskConfig"
    ) -> Union[FailedOperation, OptimizationResult]:
        """Spawns an optimization at a particular grid point and returns the result.

        Parameters
        ----------
        grid_point
            A string of the form 'dihedral_1_angle ... dihedral_n_angle' that encodes
            the current dihedrals angles to optimize at.
        job
            The flattened conformer of the molecule to start the optimization at with
            length=(n_atoms * 3)
        input_model
            The input model containing the relevant settings for how to optimize the
            structure.
        config
            The configuration to launch the task using.

        Returns
        -------
            The result of the optimization if successful, otherwise an error containing
            object.
        """

        from qcengine import compute_procedure

        input_molecule = input_model.initial_molecule.copy(deep=True).dict()
        input_molecule["geometry"] = np.array(job).reshape(len(input_molecule["symbols"]), 3)
        input_molecule = Molecule.from_data(input_molecule)

        dihedrals = input_model.keywords["dihedrals"]
        angles = grid_point.split()

        keywords = {
            **input_model.optimization_spec.keywords,
            "constraints": {
                "set": [
                    {
                        "type": "dihedral",
                        "indices": dihedral,
                        "value": int(angle),
                    }
                    for dihedral, angle in zip(dihedrals, angles)
                ]
            },
        }

        input_data = OptimizationInput(
            keywords=keywords,
            extras={},
            protocols=input_model.optimization_spec.protocols,
            input_specification=input_model.input_specification,
            initial_molecule=input_molecule,
        )

        return compute_procedure(
            input_data, procedure=input_model.optimization_spec.procedure, local_options=config.dict()
        )

    @staticmethod
    def _find_final_results(
        optimization_results: List[OptimizationResult],
    ) -> Tuple[float, Molecule]:
        """Returns the energy and final molecule of the lowest energy optimization
        in a set."""

        final_energies = np.array([result.energies[-1] for result in optimization_results])
        lowest_energy_idx = final_energies.argmin()

        return float(final_energies[lowest_energy_idx]), optimization_results[lowest_energy_idx].final_molecule