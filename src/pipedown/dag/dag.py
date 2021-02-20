from abc import abstractmethod
from copy import deepcopy
from typing import Any, Dict, List, Union

import pandas as pd

from pipedown.cross_validation.cross_validator import CrossValidator
from pipedown.cross_validation.random import RandomCrossValidator
from pipedown.dag.dag_tools import run_dag
from pipedown.dag.io import save_dag
from pipedown.nodes.base.metric import Metric
from pipedown.nodes.base.model import Model
from pipedown.nodes.base.node import Node
from pipedown.nodes.base.primary import Primary


class DAG:
    """Abstract base class for a DAG"""

    def __init__(self, name):
        super().__init__(name)
        self._nodes = None

    @abstractmethod
    def nodes(self) -> Dict[str, Node]:
        """Get a dict of node names and objects used in the DAG"""

    @abstractmethod
    def edges(self) -> Dict[str, Union[str, List[str]]]:
        """Get the edges between nodes in the DAG"""

    def fit(
        self, inputs: Dict[str, Any] = {}, outputs: Union[str, List[str]] = []
    ):
        """Fit part of or the whole pipeline"""
        self.instantiate_dag("train")
        run_dag(inputs, outputs, self.get_nodes())

    def run(
        self, inputs: Dict[str, Any] = {}, outputs: Union[str, List[str]] = []
    ):
        """Run part of or the whole pipeline"""
        self.instantiate_dag("test")
        return run_dag(inputs, outputs, self.get_nodes())

    def instantiate_dag(self, mode: str):
        """Create nodes and connections between them"""

        # Create the nodes if they don't already exist
        if self._nodes is None:
            self._nodes = self.nodes()

        # Assign names
        for node, name in self._nodes:
            node.name = name

        # Clear pre-existing connections between nodes
        for node in self._nodes:
            node.reset_children()

        # Create connections between nodes depending on mode
        for child_name, parent_names in self.edges():

            # Get actual child and parent(s) objects
            child = self.get_node(child_name)
            if isinstance(parent_names, str) and parent_names in self._nodes:
                parents = [self.get_node(parent_names)]
            elif isinstance(parent_names, list) and all(
                p in self._nodes for p in parent_names
            ):
                parents = [self.get_node(p) for p in parent_names]
            elif (
                isinstance(parent_names, dict)
                and "test" in parent_names
                and "train" in parent_names
                and isinstance(child, Primary)
            ):
                parents = [self.get_node(parent_names[mode])]
            else:
                raise RuntimeError(f"Invalid parent(s) {parent_names}")

            # Assign parents + children
            child.set_parents(parents)
            for parent in parents:
                parent.add_children(child)

    def get_nodes(self, node_type=Node) -> List[Node]:
        """Get a list of all nodes contained in this pipeline"""
        return [n for n in self._nodes.values() if isinstance(n, node_type)]

    def get_node(self, node_name: str):
        """Get a node in the pipeline by its name"""
        return self._nodes[node_name]

    def get_primary(self) -> Node:
        """Get the primary node if it exists in the DAG"""
        primaries = self.get_nodes(Primary)
        if len(primaries) < 1:
            return None
        elif len(primaries) == 1:
            return primaries[0]
        else:
            raise RuntimeError(
                "There are multiple Primary data sources in your DAG!"
            )

    def cv_predict(
        self,
        inputs: Dict[str, Any] = {},
        outputs: Union[str, List[str]] = [],
        cross_validator: CrossValidator = RandomCrossValidator(),
    ):
        """Make cross-validated predictions using the pipeline

        Parameters
        ----------
        inputs : None or pd.DataFrame
            Input to use, if any.  By default will just run the whole pipeline
            up to the model node(s) specified in `outputs`.
        outputs : List[str]
            List of names of the model nodes from which to get predictions.
        cross_validator : CrossValidator object
            Cross-validation scheme to use.

        Returns
        -------
        pd.DataFrame
            Cross-validated predictions.  Contains columns with the true target
            values (column `y_true`), and then one column for each of the
            models in `outputs` list, with that model's predictions.
        """

        # Default is to run all models in the pipeline
        if isinstance(outputs, str):
            outputs = [outputs]
        if len(outputs) == 0:
            outputs = [n.name for n in self.get_nodes(Model)]

        # Run the pipeline up to the Primary
        df, original_index = self.run_to_primary(inputs)

        # Run the cross-validation from the Primary to the output(s)
        predictions = []
        cross_validator.setup(df)
        for i in range(cross_validator.get_n_folds()):

            # Get data for this fold
            tdf = deepcopy(cross_validator.get_fold(df, i))

            # Run the pipeline for this fold
            predictions.append(
                self.run(
                    inputs={self.get_primary().name: tdf}, outputs=outputs
                )
            )

        # Return the collated predictions
        if isinstance(outputs, (list, set)) and len(outputs) > 1:
            output_predictions = ()
            for i, output in enumerate(outputs):
                output_predictions[i] = pd.concat([p[i] for p in predictions])
                output_predictions[i].sort_index(inplace=True)
                output_predictions[i].set_index(original_index)
        else:
            output_predictions = pd.concat(predictions)
            output_predictions.sort_index(inplace=True)
            output_predictions.set_index(original_index)
        return output_predictions

    def cv_metric(
        self,
        inputs: Dict[str, Any] = {},
        outputs: Union[str, List[str]] = [],
        cross_validator: CrossValidator = RandomCrossValidator(),
    ):
        """Compute metric(s) from cross-validated predictions

        Parameters
        ----------
        inputs : None or pd.DataFrame
            Input to use as the primary data.  By default will just run the
            whole pipeline up to the metric node(s) specified in `outputs`.
        outputs : List[str]
            List of names of the metric nodes to evaluate.
        cross_validator : CrossValidator object
            Cross-validation scheme to use.

        Returns
        -------
        pd.DataFrame
            Cross-validated metrics.  Contains the following columns:

            * model_name: name of the model
            * metric_name: name of the metric
            * fold: fold number
            * metric_value: value of the metric for the fold

        Notes
        -----

        You can use pipedown.visualization.plot_metrics to plot the dataframe
        returned from this method.

        .. code-block:: python3

            from pipedown.visualization import plot_metrics
            from pipedown.pipeline import Pipeline

            my_pipeline = # your pipeline

            metrics = my_pipeline.cv_metric()

            plot_metrics(metrics)

        """

        # Default is to run all metrics in the pipeline
        if isinstance(outputs, str):
            outputs = [outputs]
        if len(outputs) == 0:
            outputs = [n.name for n in self.get_nodes(Metric)]

        # Run the pipeline up to the Primary
        df, _ = self.run_to_primary(inputs)

        # Run the cross-validation from the Primary to the output(s)
        metrics = []
        cross_validator.setup(df)
        for i in range(cross_validator.get_n_folds()):

            # Get data for this fold
            tdf = deepcopy(cross_validator.get_fold(df, i))

            # Run the pipeline for this fold
            t_metrics = self.run(
                inputs={self.get_primary().name: tdf}, outputs=outputs
            )

            # Store the metrics from this fold
            for output in outputs:
                m_node = self.get_node(output)
                metrics.append(
                    {
                        "model_name": m_node.get_parents()[0].name,
                        "metric_name": m_node.get_metric_name(),
                        "fold": i,
                        "metric_value": t_metrics,
                    }
                )

        # Return the metrics
        return pd.DataFrame.from_records(metrics)

    def run_to_primary(self, inputs):
        """Run the pipeline up to the Primary"""
        primary_name = self.get_primary().name
        if primary_name in inputs:  # data for primary is already computed
            df = inputs[primary_name]
        else:  # not already computed - so we have to run the pipeline
            df = self.run(inputs=inputs, outputs=[primary_name])
        original_index = df.index
        df = df.reset_index(inplace=True, drop=True)
        return df, original_index

    def save(self, filename: str):
        """Serialize the entire pipeline"""
        save_dag(self, filename)

    def get_html(self):
        """Get html for the dashboard displaying the pipeline"""
        # TODO

    def save_html(self, filename: str):
        """Save an html file with the dashboard displaying the pipeline"""
        with open(filename, "w") as f:
            f.write(self.get_html())
