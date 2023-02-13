import os

import duckdb
import pandas as pd
import pytest
from dagster import (
    DailyPartitionsDefinition,
    MultiPartitionKey,
    MultiPartitionsDefinition,
    Out,
    StaticPartitionsDefinition,
    asset,
    graph,
    materialize,
    op,
)
from dagster._check import CheckError
from dagster_duckdb_pyspark import duckdb_pyspark_io_manager
from pyspark.sql import (
    DataFrame as SparkDF,
    SparkSession,
)


@op(out=Out(metadata={"schema": "a_df"}))
def a_df() -> SparkDF:
    spark = SparkSession.builder.getOrCreate()
    data = [(1, 4), (2, 5), (3, 6)]
    return spark.createDataFrame(data)


@op(out=Out(metadata={"schema": "add_one"}))
def add_one(df: SparkDF) -> SparkDF:
    return df.withColumn("_1", df._1 + 1)  # pylint: disable=protected-access


@graph
def make_df():
    add_one(a_df())


def test_duckdb_io_manager_with_ops(tmp_path):
    resource_defs = {
        "io_manager": duckdb_pyspark_io_manager.configured(
            {"database": os.path.join(tmp_path, "unit_test.duckdb")}
        ),
    }

    job = make_df.to_job(resource_defs=resource_defs)

    # run the job twice to ensure that tables get properly deleted
    for _ in range(2):
        res = job.execute_in_process()

        assert res.success
        duckdb_conn = duckdb.connect(database=os.path.join(tmp_path, "unit_test.duckdb"))

        out_df = duckdb_conn.execute("SELECT * FROM a_df.result").fetch_df()
        assert out_df["_1"].tolist() == [1, 2, 3]

        out_df = duckdb_conn.execute("SELECT * FROM add_one.result").fetch_df()
        assert out_df["_1"].tolist() == [2, 3, 4]

        duckdb_conn.close()


@asset(key_prefix=["my_schema"])
def b_df() -> SparkDF:
    spark = SparkSession.builder.getOrCreate()
    data = [(1, 4), (2, 5), (3, 6)]
    return spark.createDataFrame(data)


@asset(key_prefix=["my_schema"])
def b_plus_one(b_df: SparkDF) -> SparkDF:
    return b_df.withColumn("_1", b_df._1 + 1)  # pylint: disable=protected-access


def test_duckdb_io_manager_with_assets(tmp_path):
    resource_defs = {
        "io_manager": duckdb_pyspark_io_manager.configured(
            {"database": os.path.join(tmp_path, "unit_test.duckdb")}
        ),
    }

    # materialize asset twice to ensure that tables get properly deleted
    for _ in range(2):
        res = materialize([b_df, b_plus_one], resources=resource_defs)
        assert res.success

        duckdb_conn = duckdb.connect(database=os.path.join(tmp_path, "unit_test.duckdb"))

        out_df = duckdb_conn.execute("SELECT * FROM my_schema.b_df").fetch_df()
        assert out_df["_1"].tolist() == [1, 2, 3]

        out_df = duckdb_conn.execute("SELECT * FROM my_schema.b_plus_one").fetch_df()
        assert out_df["_1"].tolist() == [2, 3, 4]

        duckdb_conn.close()


@op
def non_supported_type() -> int:
    return 1


@graph
def not_supported():
    non_supported_type()


def test_not_supported_type(tmp_path):
    resource_defs = {
        "io_manager": duckdb_pyspark_io_manager.configured(
            {"database": os.path.join(tmp_path, "unit_test.duckdb")}
        ),
    }

    job = not_supported.to_job(resource_defs=resource_defs)

    with pytest.raises(
        CheckError,
        match="DuckDBIOManager does not have a handler for type '<class 'int'>'",
    ):
        job.execute_in_process()


@asset(
    partitions_def=DailyPartitionsDefinition(start_date="2022-01-01"),
    key_prefix=["my_schema"],
    metadata={"partition_expr": "time"},
    config_schema={"value": str},
)
def daily_partitioned(context) -> SparkDF:
    partition = pd.Timestamp(context.asset_partition_key_for_output())
    value = context.op_config["value"]

    pd_df = pd.DataFrame(
        {
            "time": [partition, partition, partition],
            "a": [value, value, value],
            "b": [4, 5, 6],
        }
    )
    spark = SparkSession.builder.getOrCreate()
    return spark.createDataFrame(pd_df)


def test_partitioned_asset(tmp_path):
    resource_defs = {
        "io_manager": duckdb_pyspark_io_manager.configured(
            {"database": os.path.join(tmp_path, "unit_test.duckdb")}
        )
    }

    materialize(
        [daily_partitioned],
        partition_key="2022-01-01",
        resources=resource_defs,
        run_config={"ops": {"my_schema__daily_partitioned": {"config": {"value": "1"}}}},
    )

    duckdb_conn = duckdb.connect(database=os.path.join(tmp_path, "unit_test.duckdb"))
    out_df = duckdb_conn.execute("SELECT * FROM my_schema.daily_partitioned").fetch_df()
    assert out_df["a"].tolist() == ["1", "1", "1"]
    duckdb_conn.close()

    materialize(
        [daily_partitioned],
        partition_key="2022-01-02",
        resources=resource_defs,
        run_config={"ops": {"my_schema__daily_partitioned": {"config": {"value": "2"}}}},
    )

    duckdb_conn = duckdb.connect(database=os.path.join(tmp_path, "unit_test.duckdb"))
    out_df = duckdb_conn.execute("SELECT * FROM my_schema.daily_partitioned").fetch_df()
    assert sorted(out_df["a"].tolist()) == ["1", "1", "1", "2", "2", "2"]
    duckdb_conn.close()

    materialize(
        [daily_partitioned],
        partition_key="2022-01-01",
        resources=resource_defs,
        run_config={"ops": {"my_schema__daily_partitioned": {"config": {"value": "3"}}}},
    )

    duckdb_conn = duckdb.connect(database=os.path.join(tmp_path, "unit_test.duckdb"))
    out_df = duckdb_conn.execute("SELECT * FROM my_schema.daily_partitioned").fetch_df()
    assert sorted(out_df["a"].tolist()) == ["2", "2", "2", "3", "3", "3"]
    duckdb_conn.close()


@asset(
    partitions_def=StaticPartitionsDefinition(["red", "yellow", "blue"]),
    key_prefix=["my_schema"],
    metadata={"partition_expr": "color"},
    config_schema={"value": str},
)
def static_partitioned(context) -> SparkDF:
    partition = context.asset_partition_key_for_output()
    value = context.op_config["value"]
    pd_df = pd.DataFrame(
        {
            "color": [partition, partition, partition],
            "a": [value, value, value],
            "b": [4, 5, 6],
        }
    )

    spark = SparkSession.builder.getOrCreate()
    return spark.createDataFrame(pd_df)


def test_static_partitioned_asset(tmp_path):
    duckdb_io_manager = duckdb_pyspark_io_manager.configured(
        {"database": os.path.join(tmp_path, "unit_test.duckdb")}
    )
    resource_defs = {"io_manager": duckdb_io_manager}

    materialize(
        [static_partitioned],
        partition_key="red",
        resources=resource_defs,
        run_config={"ops": {"my_schema__static_partitioned": {"config": {"value": "1"}}}},
    )

    duckdb_conn = duckdb.connect(database=os.path.join(tmp_path, "unit_test.duckdb"))
    out_df = duckdb_conn.execute("SELECT * FROM my_schema.static_partitioned").fetch_df()
    assert out_df["a"].tolist() == ["1", "1", "1"]
    duckdb_conn.close()

    materialize(
        [static_partitioned],
        partition_key="blue",
        resources=resource_defs,
        run_config={"ops": {"my_schema__static_partitioned": {"config": {"value": "2"}}}},
    )

    duckdb_conn = duckdb.connect(database=os.path.join(tmp_path, "unit_test.duckdb"))
    out_df = duckdb_conn.execute("SELECT * FROM my_schema.static_partitioned").fetch_df()
    assert sorted(out_df["a"].tolist()) == ["1", "1", "1", "2", "2", "2"]
    duckdb_conn.close()

    materialize(
        [static_partitioned],
        partition_key="red",
        resources=resource_defs,
        run_config={"ops": {"my_schema__static_partitioned": {"config": {"value": "3"}}}},
    )

    duckdb_conn = duckdb.connect(database=os.path.join(tmp_path, "unit_test.duckdb"))
    out_df = duckdb_conn.execute("SELECT * FROM my_schema.static_partitioned").fetch_df()
    assert sorted(out_df["a"].tolist()) == ["2", "2", "2", "3", "3", "3"]
    duckdb_conn.close()


@asset(
    partitions_def=MultiPartitionsDefinition(
        {
            "time": DailyPartitionsDefinition(start_date="2022-01-01"),
            "color": StaticPartitionsDefinition(["red", "yellow", "blue"]),
        }
    ),
    key_prefix=["my_schema"],
    metadata={"partition_expr": {"time": "CAST(time as TIMESTAMP)", "color": "color"}},
    config_schema={"value": str},
)
def multi_partitioned(context) -> SparkDF:
    partition = context.partition_key.keys_by_dimension
    value = context.op_config["value"]
    pd_df = pd.DataFrame(
        {
            "color": [partition["color"], partition["color"], partition["color"]],
            "time": [partition["time"], partition["time"], partition["time"]],
            "a": [value, value, value],
        }
    )

    spark = SparkSession.builder.getOrCreate()
    return spark.createDataFrame(pd_df)


def test_multi_partitioned_asset(tmp_path):
    duckdb_io_manager = duckdb_pyspark_io_manager.configured(
        {"database": os.path.join(tmp_path, "unit_test.duckdb")}
    )
    resource_defs = {"io_manager": duckdb_io_manager}

    materialize(
        [multi_partitioned],
        partition_key=MultiPartitionKey({"time": "2022-01-01", "color": "red"}),
        resources=resource_defs,
        run_config={"ops": {"my_schema__multi_partitioned": {"config": {"value": "1"}}}},
    )

    duckdb_conn = duckdb.connect(database=os.path.join(tmp_path, "unit_test.duckdb"))
    out_df = duckdb_conn.execute("SELECT * FROM my_schema.multi_partitioned").fetch_df()
    assert out_df["a"].tolist() == ["1", "1", "1"]
    duckdb_conn.close()

    materialize(
        [multi_partitioned],
        partition_key=MultiPartitionKey({"time": "2022-01-01", "color": "blue"}),
        resources=resource_defs,
        run_config={"ops": {"my_schema__multi_partitioned": {"config": {"value": "2"}}}},
    )

    duckdb_conn = duckdb.connect(database=os.path.join(tmp_path, "unit_test.duckdb"))
    out_df = duckdb_conn.execute("SELECT * FROM my_schema.multi_partitioned").fetch_df()
    assert sorted(out_df["a"].tolist()) == ["1", "1", "1", "2", "2", "2"]
    duckdb_conn.close()

    materialize(
        [multi_partitioned],
        partition_key=MultiPartitionKey({"time": "2022-01-02", "color": "red"}),
        resources=resource_defs,
        run_config={"ops": {"my_schema__multi_partitioned": {"config": {"value": "3"}}}},
    )

    duckdb_conn = duckdb.connect(database=os.path.join(tmp_path, "unit_test.duckdb"))
    out_df = duckdb_conn.execute("SELECT * FROM my_schema.multi_partitioned").fetch_df()
    assert sorted(out_df["a"].tolist()) == ["1", "1", "1", "2", "2", "2", "3", "3", "3"]
    duckdb_conn.close()

    materialize(
        [multi_partitioned],
        partition_key=MultiPartitionKey({"time": "2022-01-01", "color": "red"}),
        resources=resource_defs,
        run_config={"ops": {"my_schema__multi_partitioned": {"config": {"value": "4"}}}},
    )

    duckdb_conn = duckdb.connect(database=os.path.join(tmp_path, "unit_test.duckdb"))
    out_df = duckdb_conn.execute("SELECT * FROM my_schema.multi_partitioned").fetch_df()
    assert sorted(out_df["a"].tolist()) == ["2", "2", "2", "3", "3", "3", "4", "4", "4"]
    duckdb_conn.close()
