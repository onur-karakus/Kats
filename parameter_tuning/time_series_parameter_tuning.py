#!/usr/bin/env python3

import logging
import time
import uuid
from abc import ABC, abstractmethod
from functools import reduce
from multiprocessing.pool import Pool
from numbers import Number
from typing import Callable, Dict, List, Union

import pandas as pd
from ax import Arm, ComparisonOp, Data, OptimizationConfig, SearchSpace
from ax.core.experiment import Experiment
from ax.core.generator_run import GeneratorRun
from ax.core.metric import Metric
from ax.core.objective import Objective
from ax.core.outcome_constraint import OutcomeConstraint
from ax.modelbridge.discrete import DiscreteModelBridge
from ax.modelbridge.registry import Models
from ax.runners.synthetic import SyntheticRunner
from ax.service.utils.instantiation import (
    outcome_constraint_from_str,
    parameter_from_json,
)
from infrastrategy.kats.consts import SearchMethodEnum

# Maximum number of worker processes used to evaluate trial arms in parallel
MAX_NUM_PROCESSES = 50

class Final(type):
    def __new__(metacls, name, bases, classdict):
        for b in bases:
            if isinstance(b, Final):
                raise TypeError(
                    "type '{0}' is not an acceptable base type".format(b.__name__)
                )
        return type.__new__(metacls, name, bases, dict(classdict))


class TimeSeriesEvaluationMetric(Metric):
    def __init__(
        self,
        name: str,
        evaluation_function: Callable,
        logger: logging.Logger,
        multiprocessing: bool = False,
    ) -> None:
        super().__init__(name)
        self.evaluation_function = evaluation_function
        self.logger = logger
        self.multiprocessing = multiprocessing

    @classmethod
    def is_available_while_running(cls) -> bool:
        return True

    def evaluate_arm(self, arm) -> Dict:
        # Arm evaluation requires mean and standard error or dict for multiple metrics
        evaluation_result = self.evaluation_function(arm.parameters)
        if isinstance(evaluation_result, dict):
            return [
                {
                    "metric_name": name,
                    "arm_name": arm.name,
                    "mean": value[0],
                    "sem": value[1],
                }
                for (name, value) in evaluation_result.items()
            ]
        elif isinstance(evaluation_result, Number):
            evaluation_result = (evaluation_result, 0.0)
        elif (
            isinstance(evaluation_result, tuple)
            and len(evaluation_result) == 2
            and all(isinstance(n, Number) for n in evaluation_result)
        ):
            pass
        else:
            raise TypeError(
                "Evaluation function should either return a single numeric "
                "value that represents the error or a tuple of two numeric "
                "values, one for the mean of error and the other for the "
                "standard error of the mean of the error."
            )
        return {
            "metric_name": self.name,
            "arm_name": arm.name,
            "mean": evaluation_result[0],
            "sem": evaluation_result[1],
        }

    def fetch_trial_data(self, trial) -> Data:
        if self.multiprocessing:
            with Pool(processes=min(len(trial.arms), MAX_NUM_PROCESSES)) as pool:
                records = pool.map(self.evaluate_arm, trial.arms)
                pool.close()
        else:
            records = list(map(self.evaluate_arm, trial.arms))
        if isinstance(records[0], list):
            # Evaluation result output contains multiple metrics
            records = [metric for record in records for metric in record]
        for record in records:
            record.update({"trial_index": trial.index})
        return Data(df=pd.DataFrame.from_records(records))


class TimeSeriesParameterTuning(ABC):
    def __init__(
        self,
        parameters: List[Dict] = None,
        experiment_name: str = None,
        objective_name: str = None,
        outcome_constraints: List[str] = None,
        multiprocessing: bool = False,
    ) -> None:
        self.logger = logging.getLogger(__name__)
        self.logger.info(
            "Parameter tuning search space dimensions: {}".format(parameters)
        )
        self.validate_parameters_format(parameters)
        self.parameters = [parameter_from_json(parameter) for parameter in parameters]
        self.outcome_constraints = (
            [
                outcome_constraint_from_str(str_constraint)
                for str_constraint in outcome_constraints
            ]
            if outcome_constraints is not None
            else None
        )
        self._kats_search_space = SearchSpace(parameters=self.parameters)
        self.logger.info("Search space is created.")
        self.job_id = uuid.uuid4()
        self.experiment_name = (
            experiment_name if experiment_name else f"parameter_tuning_{self.job_id}"
        )
        self.objective_name = (
            objective_name if objective_name else f"objective_{self.job_id}"
        )
        self.multiprocessing = multiprocessing

        self._exp = Experiment(
            name=self.experiment_name,
            search_space=self._kats_search_space,
            runner=SyntheticRunner(),
        )
        self._trial_data = Data()
        self.logger.info("Experiment is created.")

    @staticmethod
    def validate_parameters_format(parameters: List) -> None:
        if not isinstance(parameters, list):
            raise TypeError(
                "The input parameter, parameters, should be a list even if a "
                "single parameter is defined."
            )
        if len(parameters) == 0:
            raise ValueError(
                "The parameter list is empty. No search space can be created "
                "if not parameter is specified."
            )
        for i, parameter_dict in enumerate(parameters):
            if not isinstance(parameter_dict, dict):
                raise TypeError(
                    "The parameter_dict, {i}, in the list of parameters should"
                    " be a dict. The parameter_dict is {parameter_dict}, and"
                    " its type is {type_}.".format(
                        i=i,
                        parameter_dict=str(parameter_dict),
                        type_=type(parameter_dict),
                    )
                )
            if len(parameter_dict) == 0:
                raise ValueError(
                    "A parameter_dict in the parameter list is empty. All "
                    "parameter_dict items should have valid key: value entries"
                    "."
                )

    def get_search_space(self):
        return self._exp.search_space

    def generator_run_for_search_method(
        self, evaluation_function: Callable, generator_run: DiscreteModelBridge
    ) -> None:
        self.evaluation_function = evaluation_function
        if self.outcome_constraints:
            # Convert dummy base Metrics to TimeseriesEvaluationMetrics
            self.outcome_constraints = [
                OutcomeConstraint(
                    TimeSeriesEvaluationMetric(
                        name=oc.metric.name,
                        evaluation_function=self.evaluation_function,
                        logger=self.logger,
                        multiprocessing=self.multiprocessing,
                    ),
                    op=oc.op,
                    bound=oc.bound,
                    relative=oc.relative,
                )
                for oc in self.outcome_constraints
            ]
        self._exp.optimization_config = OptimizationConfig(
            objective=Objective(
                metric=TimeSeriesEvaluationMetric(
                    name=self.objective_name,
                    evaluation_function=self.evaluation_function,
                    logger=self.logger,
                    multiprocessing=self.multiprocessing,
                ),
                minimize=True,
            ),
            outcome_constraints=self.outcome_constraints,
        )

        self._exp.new_batch_trial(generator_run=generator_run)
        # We run the most recent batch trial as we only run candidate trials
        self._exp.trials[max(self._exp.trials)].run()
        self._trial_data = Data.from_multiple_data([self._trial_data, self._exp.fetch_trials_data(trial_indices=[max(self._exp.trials)])])

    @abstractmethod
    def generate_evaluate_new_parameter_values(
        self,
        evaluation_function: Callable,
        arm_count: int = -1  # -1 means
        # create all arms (i.e. all combinations of parameter values)
    ) -> None:
        pass

    @staticmethod
    def _repivot_dataframe(armscore_df):
        transform = (
            armscore_df.set_index(["trial_index", "arm_name", "metric_name"])
            .unstack("metric_name")
            .reset_index()
        )
        new_cols = transform.columns.to_flat_index()
        parameters_holder = transform[
            list(filter(lambda x: "parameters" in x, new_cols))[0]
        ]
        transform.drop(columns="parameters", level=0, inplace=True)
        new_cols = new_cols.drop(labels=filter(lambda x: "parameters" in x, new_cols))
        transform.columns = ["trial_index", "arm_name"] + [
            "_".join(tpl) for tpl in new_cols[2:]
        ]
        transform["parameters"] = parameters_holder
        return transform

    def list_parameter_value_scores(
        self, legit_arms_only: bool = False
    ) -> pd.DataFrame:
        # For experiments which have not ran generate_evaluate_new_parameter_values,
        # we cannot provide trial data without metrics, so we return empty dataframe
        if not self._exp.metrics:
            return pd.DataFrame(
                [],
                columns=[
                    "arm_name",
                    "metric_name",
                    "mean",
                    "sem",
                    "parameters",
                    "trial_index",
                ],
            )
        armscore_df = self._trial_data.df.copy()
        armscore_df["parameters"] = armscore_df["arm_name"].map(
            {k: v.parameters for k, v in self._exp.arms_by_name.items()}
        )
        if self.outcome_constraints:
            # Deduplicate entries for which there are outcome constraints
            armscore_df = armscore_df.loc[armscore_df.astype(str).drop_duplicates().index]
            if legit_arms_only:

                def filter_violating_arms(
                    arms: List[Arm], data: Data, optimization_config: OptimizationConfig
                ) -> List[Arm]:
                    boolean_indices = []
                    for oc in optimization_config.outcome_constraints:
                        if oc.op is ComparisonOp.LEQ:
                            boolean_indices.append(
                                data.df[data.df.metric_name == oc.metric.name]["mean"]
                                <= oc.bound
                            )
                        else:
                            boolean_indices.append(
                                data.df[data.df.metric_name == oc.metric.name]["mean"]
                                >= oc.bound
                            )
                    eligible_arm_indices = reduce(lambda x, y: x & y, boolean_indices)
                    eligible_arm_names = data.df.loc[eligible_arm_indices.index][
                        eligible_arm_indices
                    ].arm_name
                    return list(
                        filter(lambda x: x.name in eligible_arm_names.values, arms)
                    )

                filtered_arms = filter_violating_arms(
                    list(self._exp.arms_by_name.values()),
                    self._exp.fetch_data(),
                    self._exp.optimization_config,
                )
                armscore_df = armscore_df[
                    armscore_df["arm_name"].isin([arm.name for arm in filtered_arms])
                ]
            armscore_df = self._repivot_dataframe(armscore_df)
        return armscore_df


# This class is prohibited to be derived nor instantiated. It is static, a
# factory-class to create search method objects such as GridSearch, or
# RandomSearch. Those objects are not meant to be initialized directly.
class SearchMethodFactory(metaclass=Final):
    def __init__(self):
        raise TypeError(
            "SearchMethodFactory is not allowed to be instantiated. Use "
            "it as a static class."
        )

    @staticmethod
    def create_search_method(
        parameters: List[Dict],
        selected_search_method: SearchMethodEnum = SearchMethodEnum.GRID_SEARCH,
        experiment_name: str = None,
        objective_name: str = None,
        outcome_constraints: List[str] = None,
        seed: int = None,
        bootstrap_size: int = 5,
        evaluation_function: Callable = None,
        bootstrap_arms_for_bayes_opt: List[dict] = None,
        multiprocessing: bool = False,
    ) -> TimeSeriesParameterTuning:
        """
        The method factory class that creates the search method object. It does
        not require the class to be instantiated.

        Parameters:
        ----------
        parameters: List[Dict] = None,
            Defines parameters by their names, their types their optional
            values for custom parameter search space.
        selected_search_method: SearchMethodEnum = SearchMethodEnum.GRID_SEARCH
            Defines search method to be used during parameter tuning. It has to
            be an option from the enum, SearchMethodEnum.
        experiment_name: str = None,
            Name of the experiment to be used in Ax's experiment object.
        objective_name: str = None,
            Name of the objective to be used in Ax's experiment evaluation.
        outcome_constraints: List[str] = None
            List of constraints defined as strings. Example: ['metric1 >= 0',
            'metric2 < 5]
        bootstrap_arms_for_bayes_opt: List[dict] = None
            List of params. It provides a list of self-defined inital parameter
            values for Baysian Optimal search. Example: for Holt Winter's model,
            [{'m': 7}, {'m': 14}]
        """
        if selected_search_method == SearchMethodEnum.GRID_SEARCH:
            return GridSearch(
                parameters=parameters,
                experiment_name=experiment_name,
                objective_name=objective_name,
                outcome_constraints=outcome_constraints,
                multiprocessing=multiprocessing,
            )
        elif (
            selected_search_method == SearchMethodEnum.RANDOM_SEARCH_UNIFORM
            or selected_search_method == SearchMethodEnum.RANDOM_SEARCH_SOBOL
        ):
            return RandomSearch(
                parameters=parameters,
                experiment_name=experiment_name,
                objective_name=objective_name,
                random_strategy=selected_search_method,
                outcome_constraints=outcome_constraints,
                seed=seed,
                multiprocessing=multiprocessing,
            )
        elif selected_search_method == SearchMethodEnum.BAYES_OPT:
            assert (
                evaluation_function is not None
            ), "evaluation_function cannot be None. It is needed at initialization of BayesianOptSearch object."
            return BayesianOptSearch(
                parameters=parameters,
                evaluation_function=evaluation_function,
                experiment_name=experiment_name,
                objective_name=objective_name,
                bootstrap_size=bootstrap_size,
                seed=seed,
                bootstrap_arms_for_bayes_opt=bootstrap_arms_for_bayes_opt,
                outcome_constraints=outcome_constraints,
                multiprocessing=multiprocessing,
            )
        else:
            raise NotImplementedError(
                "A search method yet to implement is selected. Only grid"
                " search and random search are implemented."
            )


class GridSearch(TimeSeriesParameterTuning):
    # Do not instantiate this class using its constructor.
    # Rather use the factory, SearchMethodFactory.
    def __init__(
        self,
        parameters: List[Dict],
        experiment_name: str = None,
        objective_name: str = None,
        outcome_constraints: List[str] = None,
        multiprocessing: bool = False,
        **kwargs,
    ) -> None:
        """
        The method factory class that creates the search method object. It does
        not require the class to be instantiated.

        Parameters:
        ----------
        parameters: List[Dict] = None,
            Defines parameters by their names, their types their optional
            values for custom parameter search space.
        experiment_name: str = None,
            Name of the experiment to be used in Ax's experiment object.
        objective_name: str = None,
            Name of the objective to be used in Ax's experiment evaluation.
        outcome_constraints: List[str] = None
            List of constraints defined as strings. Example: ['metric1 >= 0',
            'metric2 < 5]
        """
        super().__init__(
            parameters,
            experiment_name,
            objective_name,
            outcome_constraints,
            multiprocessing,
        )
        self._factorial = Models.FACTORIAL(
            search_space=self.get_search_space(), check_cardinality=False
        )
        self.logger.info("A factorial model for arm generation is created.")
        self.logger.info("A GridSearch object is successfully created.")

    def generate_evaluate_new_parameter_values(
        self,
        evaluation_function: Callable,
        arm_count: int = -1,  # -1 means create all arms (i.e. all combinations of
        # parameter values)
    ) -> None:
        if arm_count != -1:
            # FullFactorialGenerator ignores specified arm_count as it automatically determines how many arms
            self.logger.info(
                "GridSearch arm_count input is ignored and automatically determined by generator."
            )
            arm_count = -1
        factorial_run = self._factorial.gen(n=arm_count)
        self.generator_run_for_search_method(
            evaluation_function=evaluation_function, generator_run=factorial_run
        )


class RandomSearch(TimeSeriesParameterTuning):
    # Do not instantiate this class using its constructor.
    # Rather use the factory, SearchMethodFactory.
    def __init__(
        self,
        parameters: List[Dict],
        experiment_name: str = None,
        objective_name: str = None,
        seed: int = None,
        random_strategy: SearchMethodEnum = SearchMethodEnum.RANDOM_SEARCH_UNIFORM,
        outcome_constraints: List[str] = None,
        multiprocessing: bool = False,
        **kwargs,
    ) -> None:
        """
        The method factory class that creates the search method object. It does
        not require the class to be instantiated.

        Parameters:
        ----------
        parameters: List[Dict],
            Defines parameters by their names, their types their optional
            values for custom parameter search space.
        experiment_name: str = None,
            Name of the experiment to be used in Ax's experiment object.
        objective_name: str = None,
            Name of the objective to be used in Ax's experiment evaluation.
        seed: int = None,
            Seed for Ax quasi-random model. If None, then time.time() is set.
        random_strategy: SearchMethodEnum = SearchMethodEnum.RANDOM_SEARCH_UNIFORM,
            By now, we already know that the search method is random search.
            However, there are optional random strategies: UNIFORM, or SOBOL.
            This parameter allows to select it.
        outcome_constraints: List[str] = None
            List of constraints defined as strings. Example: ['metric1 >= 0',
            'metric2 < 5]
        """
        super().__init__(
            parameters,
            experiment_name,
            objective_name,
            outcome_constraints,
            multiprocessing,
        )
        if seed is None:
            seed = int(time.time())
            self.logger.info(
                "No seed is given by the user, it will be set by the current time"
            )
        self.logger.info("Seed that is used in random search: {seed}".format(seed=seed))
        if random_strategy == SearchMethodEnum.RANDOM_SEARCH_UNIFORM:
            self._random_strategy_model = Models.UNIFORM(
                search_space=self.get_search_space(), deduplicate=True, seed=seed
            )
        elif random_strategy == SearchMethodEnum.RANDOM_SEARCH_SOBOL:
            self._random_strategy_model = Models.SOBOL(
                search_space=self.get_search_space(), deduplicate=True, seed=seed
            )
        else:
            raise NotImplementedError(
                "Invalid random strategy selection. It should be either "
                "uniform or sobol."
            )
        self.logger.info(
            "A {random_strategy} model for candidate parameter value generation"
            " is created.".format(random_strategy=random_strategy)
        )
        self.logger.info("A RandomSearch object is successfully created.")

    def generate_evaluate_new_parameter_values(
        self, evaluation_function: Callable, arm_count: int = 1
    ) -> None:
        """
        This method can be called as many times as desired with arm_count in
        desired number. The total number of generated candidates will be equal
        to the their multiplication. Suppose we would like to sample k
        candidates where k = m x n such that k, m, n are integers. We can call
        this function once with `arm_count=k`, or call it k time with
        `arm_count=1` (or without that parameter at all), or call it n times
        `arm_count=m` and vice versa. They all will yield k candidates, however
        it is not guaranteed that the candidates will be identical across these
        scenarios.
        """
        model_run = self._random_strategy_model.gen(n=arm_count)
        self.generator_run_for_search_method(
            evaluation_function=evaluation_function, generator_run=model_run
        )


class BayesianOptSearch(TimeSeriesParameterTuning):
    # Do not instantiate this class using its constructor.
    # Rather use the factory, SearchMethodFactory.
    def __init__(
        self,
        parameters: List[Dict],
        evaluation_function: Callable,
        experiment_name: str = None,
        objective_name: str = None,
        bootstrap_size: int = 5,
        seed: int = None,
        random_strategy: SearchMethodEnum = SearchMethodEnum.RANDOM_SEARCH_UNIFORM,
        outcome_constraints: List[str] = None,
        multiprocessing: bool = False,
        **kwargs,
    ) -> None:
        """
        The method factory class that creates the search method object. It does
        not require the class to be instantiated.

        Parameters:
        ----------
        parameters: List[Dict],
            Defines parameters by their names, their types their optional
            values for custom parameter search space.
        evaluation_function: Callable
            The evaluation function to pass to Ax to evaluate arms.
        experiment_name: str = None,
            Name of the experiment to be used in Ax's experiment object.
        objective_name: str = None,
            Name of the objective to be used in Ax's experiment evaluation.
        bootstrap_size: int = 5,
            The number of arms that will be randomly generated to bootstrap the
            Bayesian optimization.
        seed: int = None,
            Seed for Ax quasi-random model. If None, then time.time() is set.
        random_strategy: SearchMethodEnum = SearchMethodEnum.RANDOM_SEARCH_UNIFORM,
            By now, we already know that the search method is random search.
            However, there are optional random strategies: UNIFORM, or SOBOL.
            This parameter allows to select it.
        outcome_constraints: List[str] = None
            List of constraints defined as strings. Example: ['metric1 >= 0',
            'metric2 < 5]
        """
        super().__init__(
            parameters,
            experiment_name,
            objective_name,
            outcome_constraints,
            multiprocessing,
        )
        if seed is None:
            seed = int(time.time())
            self.logger.info(
                "No seed is given by the user, it will be set by the current time"
            )
        self.logger.info("Seed that is used in random search: {seed}".format(seed=seed))
        if random_strategy == SearchMethodEnum.RANDOM_SEARCH_UNIFORM:
            self._random_strategy_model = Models.UNIFORM(
                search_space=self.get_search_space(), deduplicate=True, seed=seed
            )
        elif random_strategy == SearchMethodEnum.RANDOM_SEARCH_SOBOL:
            self._random_strategy_model = Models.SOBOL(
                search_space=self.get_search_space(), deduplicate=True, seed=seed
            )
        else:
            raise NotImplementedError(
                "Invalid random strategy selection. It should be either "
                "uniform or sobol."
            )
        self.logger.info(
            "A {random_strategy} model for candidate parameter value generation"
            " is created.".format(random_strategy=random_strategy)
        )

        bootstrap_arms_for_bayes_opt = kwargs.get("bootstrap_arms_for_bayes_opt", None)
        if bootstrap_arms_for_bayes_opt is None:
            model_run = self._random_strategy_model.gen(n=bootstrap_size)
        else:
            bootstrap_arms_list = [
                Arm(name="0_" + str(i), parameters=params)
                for i, params in enumerate(bootstrap_arms_for_bayes_opt)
            ]
            model_run = GeneratorRun(bootstrap_arms_list)

        self.generator_run_for_search_method(
            evaluation_function=evaluation_function, generator_run=model_run
        )
        self.logger.info(f'fitted data columns: {self._trial_data.df["metric_name"]}')
        self.logger.info(f"Bootstrapping of size = {bootstrap_size} is done.")

    def generate_evaluate_new_parameter_values(
        self, evaluation_function: Callable, arm_count: int = 1
    ) -> None:
        """
        This method can be called as many times as desired with arm_count in
        desired number. The total number of generated candidates will be equal
        to the their multiplication. Suppose we would like to sample k
        candidates where k = m x n such that k, m, n are integers. We can call
        this function once with `arm_count=k`, or call it k time with
        `arm_count=1` (or without that parameter at all), or call it n times
        `arm_count=m` and vice versa. They all will yield k candidates, however
        it is not guaranteed that the candidates will be identical across these
        scenarios. We re-initiate BOTORCH model on each call.
        """
        self._bayes_opt_model = Models.BOTORCH(
            experiment=self._exp,
            data=self._trial_data,
        )
        model_run = self._bayes_opt_model.gen(n=arm_count)
        self.generator_run_for_search_method(
            evaluation_function=evaluation_function, generator_run=model_run
        )


class SearchForMultipleSpaces:
    def __init__(
        self,
        parameters: Dict[str, List[Dict]],
        search_method: SearchMethodEnum = SearchMethodEnum.RANDOM_SEARCH_UNIFORM,
        experiment_name: str = None,
        objective_name: str = None,
        seed: int = None,
    ) -> None:
        """
        The method factory class that creates the search method object. It does
        not require the class to be instantiated.

        Parameters:
        ----------
        parameters: Dict[str, List[Dict]],
            Defines a search space per model. It maps model names to search spaces
        experiment_name: str = None,
            Name of the experiment to be used in Ax's experiment object.
        objective_name: str = None,
            Name of the objective to be used in Ax's experiment evaluation.
        seed: int = None,
            Seed for Ax quasi-random model. If None, then time.time() is set.
        random_strategy: SearchMethodEnum = SearchMethodEnum.RANDOM_SEARCH_UNIFORM,
            By now, we already know that the search method is random search.
            However, there are optional random strategies: UNIFORM, or SOBOL.
            This parameter allows to select it.
        """
        # search_agent_dict is a dict for str -> TimeSeriesParameterTuning object
        # Thus, we can access different search method objects created using their
        # keys.
        self.search_agent_dict = {
            agent_name: SearchMethodFactory.create_search_method(
                parameters=model_params,
                selected_search_method=search_method,
                experiment_name=experiment_name,
                objective_name=objective_name,
                seed=seed,
            )
            for agent_name, model_params in parameters.items()
        }

    def generate_evaluate_new_parameter_values(
        self, selected_model: str, evaluation_function: Callable, arm_count: int = 1
    ) -> None:
        self.search_agent_dict[selected_model].generate_evaluate_new_parameter_values(
            evaluation_function=evaluation_function, arm_count=arm_count
        )

    def list_parameter_value_scores(
        self, selected_model: str = None
    ) -> Union[pd.DataFrame, Dict[str, pd.DataFrame]]:
        if selected_model:
            return self.search_agent_dict[selected_model].list_parameter_value_scores()
        else:  # selected_model is not provided, therefore this method will
            # return a dict of data frames where each key points to the
            # parameter score values of the corresponding models.
            return {
                selected_model_: self.search_agent_dict[
                    selected_model_
                ].list_parameter_value_scores()
                for selected_model_ in self.search_agent_dict
            }