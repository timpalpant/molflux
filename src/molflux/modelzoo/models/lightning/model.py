import logging
import os
from abc import abstractmethod
from dataclasses import asdict, is_dataclass, replace
from typing import (
    Any,
)

from datasets import Dataset
from molflux.modelzoo.errors import NotTrainedError
from molflux.modelzoo.model import ModelBase
from molflux.modelzoo.typing import PredictionResult

try:
    import lightning.pytorch as pl
    import torch
except ImportError as e:
    from molflux.modelzoo.errors import ExtrasDependencyImportError

    raise ExtrasDependencyImportError("lightning", e) from e

from molflux.modelzoo.models.lightning.config import (
    CompileConfig,
    DataModuleConfig,
    LightningConfigT,
    OptimizerConfig,
    SchedulerConfig,
    TrainerConfig,
    TransferLearningConfigBase,
)
from molflux.modelzoo.models.lightning.datamodule import LightningDataModule
from molflux.modelzoo.models.lightning.module import LightningModuleBase
from molflux.modelzoo.models.lightning.utils import load_from_dvc_or_disk

logger = logging.getLogger(__name__)


class LightningModelBase(ModelBase[LightningConfigT]):
    """ABC for all PyTorch Lightning"""

    _train_multi_data_enabled: bool = True

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)

        precision = self.model_config.float32_matmul_precision
        if precision is not None:
            torch.set_float32_matmul_precision(precision)

    @property
    @abstractmethod
    def _datamodule_builder(self) -> type[LightningDataModule]:
        """The DataModule for this model class.

        Implementations should subclass from the ModelZoo LightningDataModule."""
        ...

    def _instantiate_datamodule(
        self,
        train_data: dict[str | None, Dataset] | None = None,
        validation_data: dict[str | None, Dataset] | None = None,
        test_data: dict[str | None, Dataset] | None = None,
        predict_data: Dataset | None = None,
        **kwargs: Any,
    ) -> LightningDataModule:
        """Prepares the datamodule.

        Implementations do not have to modify this method."""
        return self._datamodule_builder(
            self.model_config,
            train_data=train_data,
            validation_data=validation_data,
            test_data=test_data,
            predict_data=predict_data,
            **kwargs,
        )

    def _train(self, train_data: Dataset, **kwargs: Any) -> Any:
        """Training from single data sources is disabled for Lightning models."""
        del train_data, kwargs
        raise NotImplementedError("This class implements _train_multi_data instead.")

    def _train_multi_data(
        self,
        train_data: dict[str | None, Dataset],
        validation_data: dict[str | None, Dataset] | None = None,
        datamodule_config: DataModuleConfig | dict[str, Any] | None = None,
        trainer_config: TrainerConfig | dict[str, Any] | None = None,
        optimizer_config: OptimizerConfig | dict[str, Any] | None = None,
        scheduler_config: SchedulerConfig | dict[str, Any] | None = None,
        transfer_learning_config: TransferLearningConfigBase
        | dict[str, Any]
        | None = None,
        compile_config: CompileConfig | dict[str, Any] | bool | None = None,
        ckpt_path: str | None = None,
        **kwargs: Any,
    ) -> None:
        """
        Training method. Lightning models do not need to modify this method.

        Args:
            train_data: Dictionary of training datasets.
            validation_data: Optional validation dataset(s).
            datamodule_config: Optional dict with kwargs to temporarily override the datamodule config in the model config.
            trainer_config: Optional dict with kwargs to temporarily override the trainer config in the model config.
            optimizer_config: Optional dict with kwargs to temporarily override the optimizer config in the model config.
            scheduler_config: Optional dict with kwargs to temporarily override the scheduler config in the model config.
            transfer_learning_config: Optional dict with kwargs to temporarily override the transfer_learning_config config in the model config.
            compile_config: Optional dict with kwargs to temporarily override the compile_config config in the model config.
            ckpt_path: Optional path (local or s3) to a lightning checkpoint to resume training from. Used by the lighting trainer ``.fit`` method.
        Returns:
            None

        """
        del kwargs

        with self.override_config(
            datamodule=datamodule_config,
            trainer=trainer_config,
            transfer_learning=transfer_learning_config,
            optimizer=optimizer_config,
            scheduler=scheduler_config,
            compile=compile_config,
        ):
            if self.model_config.transfer_learning is not None:
                self._transfer_learn(train_data, validation_data)
            else:
                datamodule = self._instantiate_datamodule(
                    train_data=train_data,
                    validation_data=validation_data,
                )
                self.module: Any = self._instantiate_module()
                self._compile()
                trainer = pl.Trainer(**self.model_config.trainer.pass_to_trainer())
                trainer.fit(self.module, datamodule=datamodule, ckpt_path=ckpt_path)

    def _match_modules(
        self,
        new_module: LightningModuleBase,
        old_module: LightningModuleBase,
        modules_to_match: dict[str, str] | None,
    ) -> None:
        if modules_to_match is not None:
            list_of_old_modules = [name for name, _ in old_module.named_modules()]
            list_of_new_modules = [name for name, _ in new_module.named_modules()]

            if not (set(modules_to_match.keys()) <= set(list_of_new_modules)):
                raise KeyError(
                    f"Modules {set(modules_to_match.keys()) - set(list_of_new_modules)} not in new module.",
                )

            if not (set(modules_to_match.values()) <= set(list_of_old_modules)):
                raise KeyError(
                    f"Modules {set(modules_to_match.values()) - set(list_of_old_modules)} not in old module.",
                )

            # iterate over new module's named modules and try to match
            for name, sub_module in new_module.named_modules():
                if name in modules_to_match:
                    try:
                        sub_module.load_state_dict(
                            old_module.get_submodule(
                                modules_to_match[name],
                            ).state_dict(),
                        )
                        logger.warning(
                            f"Matched module '{name}' in new module to '{modules_to_match[name]}' in old module.\n",
                        )
                    except RuntimeError as exc:
                        raise KeyError(
                            f"Unable to match module '{name}' in new module to '{modules_to_match[name]}' in old module. {exc}",
                        ) from exc
        else:
            # try to match entire module
            try:
                new_module.load_state_dict(old_module.state_dict())
                logger.warning("Matched all of old module to new module")
            except RuntimeError as exc:
                raise KeyError(
                    f"Unable to match all of old module to new module. {exc}",
                ) from exc

    def _freeze_modules(
        self,
        module: LightningModuleBase,
        freeze_modules: list[str] | None,
    ) -> None:
        if freeze_modules is not None:
            list_of_modules = [name for name, _ in module.named_modules()]
            if not (set(freeze_modules) <= set(list_of_modules)):
                raise KeyError(
                    f"Modules {set(freeze_modules) - set(list_of_modules)} not in module.",
                )

            for name, sub_module in module.named_modules():
                if name in freeze_modules:
                    sub_module.requires_grad_(False)
                    logger.warning(f"Freezing module: {name}.\n")

    def _transfer_learn(
        self,
        train_data: dict[str | None, Dataset],
        validation_data: dict[str | None, Dataset] | None = None,
    ) -> None:
        if self.model_config.transfer_learning is None:
            return None

        # load pretrained model
        pre_trained_model = load_from_dvc_or_disk(
            path=self.model_config.transfer_learning.pre_trained_model_path,
            repo_url=self.model_config.transfer_learning.repo_url,
            rev=self.model_config.transfer_learning.rev,
            model_path_in_repo=self.model_config.transfer_learning.model_path_in_repo,
        )

        if not isinstance(pre_trained_model, LightningModelBase):
            raise RuntimeError("Pre-trained model is not a LightningModelBase.")

        self.module = self._instantiate_module()
        # match modules
        self._match_modules(
            new_module=self.module,
            old_module=pre_trained_model.module,
            modules_to_match=self.model_config.transfer_learning.modules_to_match,
        )
        ckpt_for_next_stage: dict = self.state_dict

        for stage in self.model_config.transfer_learning.stages:
            with self.override_config(
                trainer=stage.trainer,
                datamodule=stage.datamodule,
                optimizer=stage.optimizer,
                scheduler=stage.scheduler,
            ):
                datamodule = self._instantiate_datamodule(
                    train_data=train_data,
                    validation_data=validation_data,
                )

                self.module = self._instantiate_module()
                self.state_dict = ckpt_for_next_stage

                self._freeze_modules(
                    module=self.module,
                    freeze_modules=stage.freeze_modules,
                )

                self._compile()

                trainer = pl.Trainer(**self.model_config.trainer.pass_to_trainer())
                trainer.fit(self.module, datamodule=datamodule)

                # get ckpt for next stage
                ckpt_for_next_stage = self.state_dict

    def _predict(
        self,
        data: Dataset,
        datamodule_config: DataModuleConfig | dict[str, Any] | None = None,
        trainer_config: TrainerConfig | dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> PredictionResult:
        """Implements a single prediction step.

        Lightning models do not need to modify this step."""
        del kwargs

        display_names = self._predict_display_names

        # if data is empty
        if not len(data):
            return {display_name: [] for display_name in display_names}

        with self.override_config(datamodule=datamodule_config, trainer=trainer_config):
            datamodule = self._instantiate_datamodule(predict_data=data)

            trainer_config = self.model_config.trainer.pass_to_trainer()
            trainer = pl.Trainer(
                accelerator=trainer_config["accelerator"],
                devices=trainer_config["devices"],
                strategy=trainer_config["strategy"],
                logger=False,
            )

            # Expect a list of Tensors, which may need to be overwritten for some Torch models
            batch_preds: list[torch.Tensor] = trainer.predict(  # pyright: ignore
                self.module,
                datamodule,
            )

        batched_preds_catted = torch.cat(batch_preds, dim=0)

        return {
            display_name: batched_preds_catted[:, idx].tolist()
            for idx, display_name in enumerate(display_names)
        }

    @abstractmethod
    def _instantiate_module(self) -> LightningModuleBase:
        """The LightningModule attached to this model class.

        Should subclass the ModelZoo LightningModuleBase"""
        ...

    def _compile(self) -> None:
        """Compiles the Lightning model."""
        compile_config = self.model_config.compile
        if isinstance(compile_config, CompileConfig):
            self.module = torch.compile(
                self.module,
                mode=compile_config.mode,
                dynamic=compile_config.dynamic,
                fullgraph=compile_config.fullgraph,
                backend=compile_config.backend,
                **compile_config.backend_kwargs,
            )

    @property
    def config(self) -> dict[str, Any]:
        return asdict(self.model_config)

    def override_config(self, **section_overrides: Any) -> "ConfigOverride":
        """Context manager for temporarily overriding config, e.g. during
        transfer learning."""
        return ConfigOverride(self, **section_overrides)

    @property
    def is_compiled(self) -> bool:
        return hasattr(self, "module") and hasattr(self.module, "_orig_mod")

    @property
    def state_dict(self) -> Any:
        module = self.module if not self.is_compiled else self.module._orig_mod
        return module.state_dict()

    @state_dict.setter
    def state_dict(self, state_dict: dict[str, torch.Tensor]) -> None:
        module = self.module if not self.is_compiled else self.module._orig_mod
        module.load_state_dict(state_dict)

    def as_dir(self, directory: str) -> None:
        """Saves the model into a directory."""

        if hasattr(self, "module") and self.module is not None:
            ckpt = {"state_dict": self.state_dict}
            torch.save(ckpt, os.path.join(directory, "module_checkpoint.ckpt"))
        else:
            raise NotTrainedError("No initialised module to save.")

    def from_dir(self, directory: str) -> None:
        """Loads a model from a directory."""

        ckpt_path = os.path.join(directory, "module_checkpoint.ckpt")
        checkpoint = torch.load(ckpt_path, map_location="cpu", weights_only=True)

        self.module = self._instantiate_module()
        self.state_dict = checkpoint["state_dict"]


class ConfigOverride:
    def __init__(
        self,
        model: LightningModelBase[LightningConfigT],
        **section_overrides: dict[str, Any] | bool,
    ):
        self.model = model
        self.section_overrides = {
            section: override
            for section, override in section_overrides.items()
            if override is not None
        }
        self.original_config = model.model_config

    def __enter__(self) -> None:
        new_sections = {}
        for section, override in self.section_overrides.items():
            old_section = getattr(self.original_config, section)

            # Checks if override is a dataclass instance (not type) with suitable type
            override_obj: bool | dict[str, Any]
            if (
                is_dataclass(override)
                and not isinstance(override, type)  # type: ignore[unreachable]
                and (
                    isinstance(override, type(old_section))
                    or (old_section is None)
                    or isinstance(old_section, bool)
                )
            ):
                override_obj = asdict(override)  # type: ignore[unreachable]
            elif isinstance(override, dict) or isinstance(override, bool):
                override_obj = override
            else:
                raise RuntimeError(
                    f"Unsure how to override {type(old_section)} with object of type {type(override)}",
                )

            if (
                old_section is not None
                and not isinstance(old_section, bool)
                and not isinstance(override_obj, bool)
            ):
                new_sections[section] = replace(old_section, **override_obj)
            else:
                new_sections[section] = override_obj

        # Mypy has trouble recognising TypeVar bound to a Pydantic dataclass
        new_model_config = replace(
            self.original_config,
            **new_sections,
        )

        # NOTE model_config is stored in the model, module and datamodule
        # It will be correctly overridden ONLY in these two locations.
        # Datamodules are always instantiated UNDER the override and so are
        # passed an overridden config.
        self.model.model_config = new_model_config

        if hasattr(self.model, "module") and self.model.module is not None:
            self.model.module.model_config = new_model_config

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: Any | None,
    ) -> None:
        del exc_type, exc_val, exc_tb
        self.model.model_config = self.original_config

        if hasattr(self.model, "module") and self.model.module is not None:
            self.model.module.model_config = self.original_config
