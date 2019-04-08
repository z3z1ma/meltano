import pytest
import json
import os
import shutil

from pathlib import Path
from os.path import join

from meltano.core.compiler.project_compiler import ProjectCompiler
from meltano.core.sql.sql_utils import SqlUtils
from meltano.core.sql.analysis_helper import AnalysisHelper
from meltano.core.sql.date import Date
from meltano.core.project import Project
from meltano.core.m5o.m5o_file_parser import MeltanoAnalysisFileParser
from meltano.core.m5o.m5oc_file import M5ocFile
from meltano.core.sql.base import (
    MeltanoDesign,
    MeltanoTable,
    MeltanoColumn,
    MeltanoAggregate,
    MeltanoQuery,
)


@pytest.fixture(scope="class")
def setup_test_models(project, request):
    # Find where the subfolder with the tmp models is by using request.fspath
    #  as we switch to another directory for tests (check test_dir session fixture)
    models_dir = request.fspath.join("..").join("models/")

    # And copy everything to the model/ directory of the test Project
    project_models_dir = project.root_dir("model")

    test_models = os.listdir(models_dir)
    for file_name in test_models:
        full_file_name = os.path.join(models_dir, file_name)
        if os.path.isfile(full_file_name):
            shutil.copy(full_file_name, project_models_dir)


@pytest.fixture(scope="class")
def design_helper(project, setup_test_models):
    # Compile the test files and make sure that the proper m5oc file was generated
    ProjectCompiler(project).compile()

    # Load the m5oc file for gitflix
    m5oc_file = project.root_dir("model", "gitflix.topic.m5oc")
    m5oc = M5ocFile.load(m5oc_file)
    return m5oc.design("users")


@pytest.fixture(scope="module")
def query_payload():
    payload = """
    {
     "run":true,
     "table":"users",
     "columns":["gender"],
     "aggregates":["count", "avg_age", "sum_clv"],
     "timeframes":[],
     "joins":[
      {"name":"streams","columns":["day", "month", "year"],"aggregates":["count", "sum_minutes", "count_days"]},
      {"name":"episodes","columns":["tv_series"],"aggregates":["count", "avg_rating"]}
     ],
     "order":null,
     "limit":"50",
     "filters":{}
    }
    """
    return json.loads(payload)


class TestQueryGeneration:
    def test_compile_and_load_m5o_files(self, project, design_helper):
        design = MeltanoDesign(definition=design_helper.design)

        assert design.name == "users"
        assert len(design.tables()) == 3
        assert len(design.joins()) == 2

    def test_meltano_base_classes(self, design_helper):
        design = MeltanoDesign(definition=design_helper.design)

        assert design.name == "users"
        assert len(design.tables()) == 3
        assert len(design.joins()) == 2

        table = design.get_table("streams")
        assert table.name == "streams"

        join = design.get_join("episodes")
        assert join.name == "episodes"

        # Test Meltano Tables and Columns
        assert "id" in [c.column_name() for c in table.primary_keys()]
        assert "streams.month" in [c.alias() for c in table.columns()]

        assert len(table.columns()) == 6
        new_c = MeltanoColumn(table)
        new_c.copy_metadata(table.get_column("month"))
        new_c.name = "quarter"
        table.add_column(new_c)
        assert len(table.columns()) == 7
        assert table.get_column("quarter").type == table.get_column("month").type

        # Test Meltano Tables and Aggregates
        assert "minutes" in [a.column_name() for a in table.aggregates()]
        assert "streams.id" in [a.column_alias() for a in table.aggregates()]
        assert "streams.sum_minutes" in [a.alias() for a in table.aggregates()]
        assert "streams.count_days" in [a.alias() for a in table.aggregates()]

        assert len(table.aggregates()) == 3
        new_a = MeltanoAggregate(table)
        new_a.name = "avg_price"
        new_a.type = "avg"
        new_a.label = "AVG Price"
        new_a.description = "Average Price"
        new_a.sql = "{{table}}.price"
        table.add_aggregate(new_a)
        assert len(table.aggregates()) == 4

    def test_meltano_query(self, design_helper, query_payload):
        # Test parsing a json payload using a Design generated from a m5oc file
        #  and generating a proper MeltanoQuery Object
        q = MeltanoQuery(definition=query_payload, design_helper=design_helper)

        assert q.design.name == "users"
        assert len(q.tables) == 3
        assert len(q.join_order) == 3
        assert q.join_order[2]["table"] == "episodes"

        # Test generating an HDA query
        (sql, column_headers, column_names, aggregate_columns) = q.hda_query()
        assert "Average Age" in column_headers
        assert "sum_minutes" in column_names
        assert "users.sum_clv" in aggregate_columns

        assert "WITH base_join AS (SELECT" in sql
        assert "base_streams AS (SELECT DISTINCT" in sql
        assert "users_stats AS (" in sql
        assert 'COALESCE(AVG("episodes.rating"),0)' in sql
        assert 'COALESCE(COUNT("users.id"),0)' in sql
        assert 'COALESCE(SUM("users.clv"),0)' in sql
        assert 'SELECT * FROM "result"' in sql

        # Check that the attribute used both as a column and as an aggregate
        # 1. Only appears once in the select clause of the base query
        # 2. Properly appears as an aggregate
        assert sql.count('"streams"."day" "streams.day"') == 1
        assert 'COALESCE(COUNT("streams.day"),0)' in sql
