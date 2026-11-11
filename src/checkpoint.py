import hashlib
import pickle


def signature(value, seen=None):
    seen = {} if seen is None else seen
    if value is None or isinstance(value, (bool, int, float, str)):
        return (type(value).__name__, repr(value))
    if isinstance(value, bytes):
        return ("bytes", hashlib.sha256(value).hexdigest())
    ident = id(value)
    if ident in seen:
        return ("alias", seen[ident][0])
    seen[ident] = (len(seen), value)
    if isinstance(value, dict):
        return (
            "dict",
            [(signature(k, seen), signature(v, seen)) for (k, v) in value.items()],
        )
    if isinstance(value, (list, tuple)):
        return (type(value).__name__, [signature(v, seen) for v in value])
    if isinstance(value, set):
        return ("set", sorted((repr(signature(v, {})) for v in value)))
    try:
        import pandas as pd
        import numpy as np

        if isinstance(value, pd.DataFrame):
            try:
                contents = hashlib.sha256(
                    pd.util.hash_pandas_object(
                        value, index=True, categorize=True
                    ).values.tobytes()
                ).hexdigest()
            except TypeError:
                contents = hashlib.sha256(
                    pickle.dumps(value.to_dict("split"), protocol=5)
                ).hexdigest()
            categories = [
                (
                    str(c),
                    signature(value[c].cat.categories.tolist(), seen),
                    value[c].cat.ordered,
                )
                for c in value.columns
                if isinstance(value[c].dtype, pd.CategoricalDtype)
            ]
            return (
                "DataFrame",
                signature(value.columns.tolist(), seen),
                str(type(value.index)),
                signature(value.index.names, seen),
                [str(x) for x in value.dtypes],
                signature(value.attrs, seen),
                contents,
                categories,
            )
        if isinstance(value, np.ndarray):
            return (
                "ndarray",
                str(value.dtype),
                value.shape,
                signature(value.tolist(), seen)
                if value.dtype.hasobject
                else hashlib.sha256(value.tobytes()).hexdigest(),
            )
        if isinstance(value, np.generic):
            return ("numpy", str(value.dtype), signature(value.item(), seen))
    except ImportError:
        pass
    return (
        type(value).__module__,
        type(value).__qualname__,
        hashlib.sha256(pickle.dumps(value, protocol=5)).hexdigest(),
    )
