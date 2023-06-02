import tqdm

import numpy as np
import datamol as dm
import seaborn as sns
import pandas as pd

from sklearn.model_selection import ShuffleSplit
from dataclasses import dataclass
from loguru import logger
from typing import Union, List, Optional, Callable, Dict

from scipy.stats import gaussian_kde
from scipy.spatial.distance import jensenshannon
from sklearn.metrics import pairwise_distances
from sklearn.neighbors import NearestNeighbors
from sklearn.model_selection import BaseShuffleSplit, GroupShuffleSplit
from sklearn.model_selection._split import _validate_shuffle_split, _num_samples
from sklearn.cluster import MiniBatchKMeans

from mood.transformer import EmpiricalKernelMapTransformer
from mood.distance import get_distance_metric
from mood.visualize import plot_distance_distributions
from mood.utils import get_outlier_bounds


MOOD_SPLITTERS = ["Random", "Scaffold", "Perimeter", "Maximum Dissimilarity"]


def get_mood_splitters(smiles, n_splits: int = 5, random_state: int = 0, n_jobs: Optional[int] = None):
    scaffolds = [dm.to_smiles(dm.to_scaffold_murcko(dm.to_mol(smi))) for smi in smiles]
    splitters = {
        "Random": ShuffleSplit(n_splits=n_splits, random_state=random_state),
        "Scaffold": PredefinedGroupShuffleSplit(
            groups=scaffolds, n_splits=n_splits, random_state=random_state
        ),
        "Perimeter": PerimeterSplit(
            n_clusters=25, n_splits=n_splits, random_state=random_state, n_jobs=n_jobs
        ),
        "Maximum Dissimilarity": MaxDissimilaritySplit(
            n_clusters=25, n_splits=n_splits, random_state=random_state, n_jobs=n_jobs
        ),
    }
    return splitters


@dataclass
class SplitCharacterization:
    """
    Within the context of MOOD, a split is characterized by
    a distribution of distances and an associated representativeness score
    """

    distances: np.ndarray
    representativeness: float
    label: str

    @classmethod
    def concat(cls, splits):
        names = set([obj.label for obj in splits])
        if len(names) != 1:
            raise RuntimeError("Can only concatenate equally labeled split characterizations")

        dist = np.concatenate([obj.distances for obj in splits])
        score = np.mean([obj.representativeness for obj in splits])
        return cls(dist, score, names.pop())

    @staticmethod
    def best(splits):
        return max(splits, key=lambda spl: spl.representativeness)

    @staticmethod
    def as_dataframe(splits):
        df = pd.DataFrame()
        best = SplitCharacterization.best(splits)
        for split in splits:
            df_ = pd.DataFrame(
                {
                    "split": split.label,
                    "representativeness": split.representativeness,
                    "best": split == best,
                },
                index=[0],
            )
            df = pd.concat((df, df_), ignore_index=True)
        df["rank"] = df["representativeness"].rank(ascending=False)
        return df

    def __eq__(self, other):
        return self.label == other.label and self.representativeness == other.representativeness

    def __repr__(self):
        return self.__str__()

    def __str__(self):
        return f"{self.__class__.__name__}[{self.label}]"


class MOODSplitter(BaseShuffleSplit):
    """
    The MOOD splitter takes in multiple splitters and a set of
    downstream molecules and prescribes one splitting method
    that creates the test set that is most representative of
    downstream applications.
    """

    def __init__(
        self,
        splitters: Dict[str, BaseShuffleSplit],
        downstream_distances: Optional[np.ndarray] = None,
        metric: Union[str, Callable] = "minkowski",
        p: int = 2,
        k: int = 5,
    ):
        """
        Args:
            splitters: A list of splitter methods you are considering
            downstream_distances: A list of precomputed distances for the downstream application
            metric: The distance metric to use
            p: If the metric is the minkowski distance, this is the p in that distance.
            k: The number of nearest neighbors to use to compute the distance.
        """
        super().__init__()
        if not all(isinstance(obj, BaseShuffleSplit) for obj in splitters.values()):
            raise TypeError("All splitters should be BaseShuffleSplit objects")

        n_splits_per_splitter = [obj.get_n_splits() for obj in splitters.values()]
        if not len(set(n_splits_per_splitter)) == 1:
            raise TypeError("n_splits is inconsistent across the different splitters")
        self._n_splits = n_splits_per_splitter[0]

        self._p = p
        self._k = k
        self._metric = metric
        self._splitters = splitters
        self._downstream_distances = downstream_distances

        self._split_chars = None
        self._prescribed_splitter_label = None

    @staticmethod
    def visualize(downstream_distances: np.ndarray, splits: List[SplitCharacterization], ax: Optional = None):
        splits = sorted(splits, key=lambda spl: spl.representativeness)
        cmap = sns.color_palette("rocket", len(splits) + 1)

        distances = [spl.distances for spl in splits]
        colors = [cmap[rank + 1] for rank, spl in enumerate(splits)]
        labels = [spl.label for spl in splits]

        ax = plot_distance_distributions(distances, labels, colors, ax=ax)

        lower, upper = get_outlier_bounds(downstream_distances, factor=3.0)
        mask = (downstream_distances >= lower) & (downstream_distances <= upper)
        downstream_distances = downstream_distances[mask]

        sns.kdeplot(downstream_distances, color=cmap[0], linestyle="--", alpha=0.3, ax=ax)
        return ax

    @staticmethod
    def score_representativeness(downstream_distances, distances, num_samples: int = 100):
        """Scores a representativeness score between two distributions
        A higher score should be interpreted as _more_ representative"""
        pdf_split = gaussian_kde(distances)
        pdf_downstream = gaussian_kde(downstream_distances)

        vmin = np.min(np.concatenate((downstream_distances, distances)))
        vmax = np.max(np.concatenate((downstream_distances, distances)))
        positions = np.linspace(vmin, vmax, num=num_samples)

        samples_split = pdf_split(positions)
        samples_downstream = pdf_downstream(positions)

        return 1.0 - jensenshannon(samples_downstream, samples_split, base=2)

    @property
    def prescribed_splitter_label(self):
        if not self.fitted:
            raise RuntimeError("The splitter has not be fitted yet")
        return self._prescribed_splitter_label

    @property
    def fitted(self):
        return self._prescribed_splitter_label is not None

    def _compute_distance(self, X_from, X_to):
        """
        Computes the k-NN distance from one set to another

        Args:
            X_from: The set to compute the distance for
            X_to: The set to compute the distance to (i.e. the neighbor candidates)
        """
        knn = NearestNeighbors(n_neighbors=self._k, metric=self._metric, p=self._p).fit(X_to)
        distances, ind = knn.kneighbors(X_from)
        distances = np.mean(distances, axis=1)
        return distances

    def get_prescribed_splitter(self):
        return self._splitters[self.prescribed_splitter_label]

    def get_protocol_results(self):
        return SplitCharacterization.as_dataframe(self._split_chars)

    def fit(self, X, y=None, groups=None, X_deployment=None, plot: bool = False, progress: bool = False):
        """Follows the MOOD specification to prescribe a train-test split
        that is most representative of downstream applications.

        In MOOD, the k-NN distance in the representation space functions
        as a proxy of difficulty. The further a datapoint is from the training
        set, in general the lower a model's performance. Using that observation,
        we prescribe the train-test split that best replicates the distance
        distribution (i.e. "the difficulty") of a downstream application.
        """

        if self._downstream_distances is None:
            self._downstream_distances = self._compute_distance(X_deployment, X)

        # Precompute all splits. Since splitters are implemented as generators,
        # we store the resulting splits so we can replicate them later on.
        split_chars = list()

        it = self._splitters.items()
        if progress:
            it = tqdm.tqdm(it, desc="Splitter")

        for name, splitter in it:
            # We possibly repeat the split multiple times to
            # get a more reliable  estimate
            chars = []

            it_ = splitter.split(X, y, groups)
            if progress:
                it_ = tqdm.tqdm(it_, leave=False, desc="Split", total=self._n_splits)

            for split in it_:
                train, test = split
                distances = self._compute_distance(X[test], X[train])
                distances = distances[np.isfinite(distances)]
                distances = distances[~np.isnan(distances)]

                score = self.score_representativeness(self._downstream_distances, distances)
                chars.append(SplitCharacterization(distances, score, name))

            split_chars.append(SplitCharacterization.concat(chars))

        # Rank different splitting methods by their ability to
        # replicate the downstream distance distribution.
        chosen = SplitCharacterization.best(split_chars)

        self._split_chars = split_chars
        self._prescribed_splitter_label = chosen.label

        logger.info(
            f"Ranked all different splitting methods:\n{SplitCharacterization.as_dataframe(split_chars)}"
        )
        logger.info(f"Selected {chosen.label} as the most representative splitting method")

        if plot:
            # Visualize the results
            return self.visualize(self._downstream_distances, split_chars)

    def _iter_indices(self, X=None, y=None, groups=None):
        """Generate (train, test) indices"""
        if not self.fitted:
            raise RuntimeError("The splitter has not be fitted yet")
        yield from self.get_prescribed_splitter()._iter_indices(X, y, groups)


class PredefinedGroupShuffleSplit(GroupShuffleSplit):
    """Simple class that tackles the limitation of the MOODSplitter
    that all splitters need to use the same grouping."""

    def __init__(self, groups, n_splits=5, *, test_size=None, train_size=None, random_state=None):
        super().__init__(
            n_splits=n_splits,
            test_size=test_size,
            train_size=train_size,
            random_state=random_state,
        )
        self._groups = groups

    def _iter_indices(self, X=None, y=None, groups=None):
        """Generate (train, test) indices"""
        if groups is not None:
            logger.warning("Ignoring the groups parameter in favor of the predefined groups")
        yield from super()._iter_indices(X, y, self._groups)


class KMeansSplit(GroupShuffleSplit):
    """Split based on the k-Mean clustering in input space"""

    def __init__(
        self, n_clusters: int = 10, n_splits: int = 5, *, test_size=None, train_size=None, random_state=None
    ):
        super().__init__(
            n_splits=n_splits,
            test_size=test_size,
            train_size=train_size,
            random_state=random_state,
        )
        self._n_clusters = n_clusters

    def compute_kmeans_clustering(self, X, random_state_offset: int = 0, return_centers: bool = False):
        metric = get_distance_metric(X)

        if self.random_state is not None:
            seed = self.random_state + random_state_offset
        else:
            seed = None

        if metric != "euclidean":
            logger.debug(f"To use KMeans with the {metric} metric, we use the Empirical Kernel Map")
            transformer = EmpiricalKernelMapTransformer(
                n_samples=min(512, len(X)),
                metric=metric,
                random_state=seed,
            )
            X = transformer(X)

        model = MiniBatchKMeans(self._n_clusters, random_state=seed, compute_labels=True)
        model.fit(X)

        indices = model.labels_
        if not return_centers:
            return indices

        centers = model.cluster_centers_[indices]
        return indices, centers

    def _iter_indices(self, X=None, y=None, groups=None):
        """Generate (train, test) indices"""
        if groups is not None:
            logger.warning("Ignoring the groups parameter in favor of the predefined groups")
        groups = self.compute_kmeans_clustering(X)
        yield from super()._iter_indices(X, y, groups)


class PerimeterSplit(KMeansSplit):
    """
    Places the pairs of data points with maximal pairwise distance in the test set.
    This was originally called the extrapolation-oriented split, introduced in  Szántai-Kis et. al., 2003
    """

    def __init__(
        self,
        n_clusters: int = 10,
        n_splits: int = 5,
        n_jobs: Optional[int] = None,
        *,
        test_size=None,
        train_size=None,
        random_state=None,
    ):
        super().__init__(
            n_clusters=n_clusters,
            n_splits=n_splits,
            test_size=test_size,
            train_size=train_size,
            random_state=random_state,
        )
        self._n_jobs = n_jobs

    def _iter_indices(self, X, y=None, groups=None):
        if groups is not None:
            logger.warning("Ignoring the groups parameter in favor of the predefined groups")

        n_samples = _num_samples(X)
        n_train, n_test = _validate_shuffle_split(
            n_samples,
            self.test_size,
            self.train_size,
            default_test_size=self._default_test_size,
        )

        for i in range(self.n_splits):
            groups, centers = self.compute_kmeans_clustering(X, random_state_offset=i, return_centers=True)
            centers, group_indices, group_counts = np.unique(
                centers, return_inverse=True, return_counts=True, axis=0
            )
            groups_set = np.unique(group_indices)

            # We always use the euclidean metric. For binary vectors we would have
            # used the jaccard metric normally, but because of the k-Means clustering this
            # data would be transformed using the Empirical Kernel Map.
            distance_matrix = pairwise_distances(centers, metric="euclidean", n_jobs=self._n_jobs)

            # Sort the distance matrix to find the groups that are the furthest away from one another
            tril_indices = np.tril_indices_from(distance_matrix, k=-1)
            maximum_distance_indices = np.argsort(distance_matrix[tril_indices])[::-1]

            test_indices = []
            remaining = set(groups_set)

            for pos in maximum_distance_indices:
                if len(test_indices) >= n_test:
                    break

                i, j = (
                    tril_indices[0][pos],
                    tril_indices[1][pos],
                )

                # If one of the molecules in this pair is already in the test set, skip to the next
                if not (i in remaining and j in remaining):
                    continue

                remaining.remove(i)
                test_indices.extend(list(np.flatnonzero(group_indices == groups_set[i])))
                remaining.remove(j)
                test_indices.extend(list(np.flatnonzero(group_indices == groups_set[j])))

            train_indices = []
            for i in remaining:
                train_indices.extend(list(np.flatnonzero(group_indices == groups_set[i])))

            yield np.array(train_indices), np.array(test_indices)


class MaxDissimilaritySplit(KMeansSplit):
    """Splits the data such that the train and test set are maximally dissimilar."""

    def __init__(
        self,
        n_clusters: int = 10,
        n_splits: int = 5,
        n_jobs: Optional[int] = None,
        *,
        test_size=None,
        train_size=None,
        random_state=None,
    ):
        super().__init__(
            n_clusters=n_clusters,
            n_splits=n_splits,
            test_size=test_size,
            train_size=train_size,
            random_state=random_state,
        )
        self._n_jobs = n_jobs

    def _iter_indices(self, X, y=None, groups=None):
        """Generate (train, test) indices"""

        if groups is not None:
            logger.warning("Ignoring the groups parameter in favor of the predefined groups")

        metric = get_distance_metric(X)

        n_samples = _num_samples(X)
        n_train, n_test = _validate_shuffle_split(
            n_samples,
            self.test_size,
            self.train_size,
            default_test_size=self._default_test_size,
        )

        for i in range(self.n_splits):
            # We introduce some stochasticity through the k-Means clustering
            groups, centers = self.compute_kmeans_clustering(X, random_state_offset=i, return_centers=True)
            centers, group_indices, group_counts = np.unique(
                centers, return_inverse=True, return_counts=True, axis=0
            )
            groups_set = np.unique(group_indices)

            # We always use the euclidean metric. For binary vectors we would have
            # used the jaccard metric normally, but because of the k-Means clustering this
            # data would be transformed using the Empirical Kernel Map.
            distance_matrix = pairwise_distances(centers, metric="euclidean", n_jobs=self._n_jobs)

            # The initial test cluster is the one with the
            # highest mean distance to all other clusters
            test_idx = np.argmax(distance_matrix.mean(axis=0))

            # The initial train cluster is the one furthest from
            # the initial test cluster
            train_idx = np.argmax(distance_matrix[test_idx])

            train_indices = np.flatnonzero(group_indices == groups_set[train_idx])
            test_indices = np.flatnonzero(group_indices == groups_set[test_idx])

            # Iteratively add the train cluster that is furthest
            # from the _initial_ test cluster.
            sorted_groups = np.argsort(distance_matrix[train_idx])
            for group_idx in sorted_groups:
                if len(train_indices) >= n_train:
                    break

                if group_idx == train_idx or group_idx == test_idx:
                    continue

                indices_to_add = np.flatnonzero(group_indices == groups_set[group_idx])
                train_indices = np.concatenate([train_indices, indices_to_add])

            # Construct test set
            remaining_groups = list(set(range(n_samples)) - set(train_indices) - set(test_indices))
            test_indices = np.concatenate([test_indices, remaining_groups]).astype(int)

            yield train_indices, test_indices
