import datetime
import uuid
import os
import pytest
from notebooker.execute_notebook import _run_checks
from notebooker import notebook_templates_example
from ..utils import all_templates


@pytest.fixture(scope="module")
def py_template_base_dir():
    return os.path.abspath(notebook_templates_example.__path__[0])


@pytest.mark.parametrize("template_name", all_templates())
def test_execution_of_templates(template_name, template_dir, output_dir, flask_app, py_template_base_dir):
    with flask_app.app_context():
        _run_checks(
            "job_id_{}".format(str(uuid.uuid4())[:6]),
            datetime.datetime.now(),
            template_name,
            template_name,
            output_dir,
            template_dir,
            {},
            py_template_base_dir=py_template_base_dir,
            generate_pdf_output=False,
        )
