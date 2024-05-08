from __future__ import unicode_literals

import json

import traceback
from logging import getLogger
from typing import Any, Dict, List, Tuple, NamedTuple, Optional, AnyStr

import nbformat
from flask import Blueprint, abort, jsonify, render_template, request, url_for, current_app
from nbformat import NotebookNode

from notebooker.constants import DEFAULT_RESULT_LIMIT
from notebooker.execute_notebook import run_report_in_subprocess
from notebooker.settings import WebappConfig
from notebooker.utils.conversion import generate_ipynb_from_py
from notebooker.utils.results import get_all_result_keys
from notebooker.utils.templates import _get_parameters_cell_idx, _get_preview
from notebooker.utils.web import convert_report_name_url_to_path, json_to_python, validate_mailto, validate_title
from notebooker.web.handle_overrides import handle_overrides
from notebooker.web.utils import get_serializer, _get_python_template_dir, get_all_possible_templates

try:
    FileNotFoundError
except NameError:
    FileNotFoundError = IOError

run_report_bp = Blueprint("run_report_bp", __name__)
logger = getLogger(__name__)


@run_report_bp.route("/run_report/get_preview/<path:report_name>", methods=["GET"])
def run_report_get_preview(report_name):
    """
    Get a preview of the Notebook Template which is about to be executed.

    :param report_name: The parameter here should be a "/"-delimited string which mirrors the directory structure of \
        the notebook templates.

    :returns: An HTML rendering of a notebook template which has been converted from .py -> .ipynb -> .html
    """
    report_name = convert_report_name_url_to_path(report_name)
    # Handle the case where a rendered ipynb asks for "custom.css"
    if ".css" in report_name:
        return ""
    return _get_preview(
        report_name,
        notebooker_disable_git=current_app.config["NOTEBOOKER_DISABLE_GIT"],
        py_template_dir=_get_python_template_dir(),
    )


def get_report_as_nb(relative_report_path: str) -> NotebookNode:
    path = generate_ipynb_from_py(
        current_app.config["TEMPLATE_DIR"],
        relative_report_path,
        current_app.config["NOTEBOOKER_DISABLE_GIT"],
        _get_python_template_dir(),
    )
    nb = nbformat.read(path, as_version=nbformat.v4.nbformat)
    return nb


def get_report_parameters_html(relative_report_path: str) -> str:
    nb = get_report_as_nb(relative_report_path)
    metadata_idx = _get_parameters_cell_idx(nb)
    parameters_as_html = ""
    if metadata_idx is not None:
        metadata = nb["cells"][metadata_idx]
        parameters_as_html = metadata["source"].strip()
    return parameters_as_html


@run_report_bp.route("/run_report/<path:report_name>", methods=["GET"])
def run_report_http(report_name):
    """
    The "Run Report" interface is generated by this method.

    :param report_name: The parameter here should be a "/"-delimited string which mirrors the directory structure of \
        the notebook templates.

    :returns: An HTML template which is the Run Report interface.
    """
    report_name = convert_report_name_url_to_path(report_name)
    json_params = request.args.get("json_params")
    initial_python_parameters = json_to_python(json_params) or ""
    try:
        nb = get_report_as_nb(report_name)
    except FileNotFoundError:
        logger.exception("Report was not found.")
        return render_template(
            "run_report.html",
            report_found=False,
            parameters_as_html="REPORT NOT FOUND",
            has_prefix=False,
            has_suffix=False,
            report_name=report_name,
            all_reports=get_all_possible_templates(),
            initialPythonParameters={},
            readonly_mode=current_app.config["READONLY_MODE"],
            scheduler_disabled=current_app.config["DISABLE_SCHEDULER"],
        )
    metadata_idx = _get_parameters_cell_idx(nb)
    has_prefix = has_suffix = False
    if metadata_idx is not None:
        has_prefix, has_suffix = (bool(nb["cells"][:metadata_idx]), bool(nb["cells"][metadata_idx + 1 :]))
    return render_template(
        "run_report.html",
        parameters_as_html=get_report_parameters_html(report_name),
        report_found=True,
        has_prefix=has_prefix,
        has_suffix=has_suffix,
        report_name=report_name,
        all_reports=get_all_possible_templates(),
        initialPythonParameters=initial_python_parameters,
        default_mailfrom=current_app.config["DEFAULT_MAILFROM"],
        readonly_mode=current_app.config["READONLY_MODE"],
        scheduler_disabled=current_app.config["DISABLE_SCHEDULER"],
    )


class RunReportParams(NamedTuple):
    report_title: AnyStr
    mailto: AnyStr
    error_mailto: AnyStr
    mailfrom: AnyStr
    generate_pdf_output: bool
    hide_code: bool
    scheduler_job_id: Optional[str]
    is_slideshow: bool
    email_subject: Optional[str]


def validate_run_params(report_name, params, issues: List[str]) -> RunReportParams:
    logger.info(f"Validating input params: {params} for {report_name}")
    # Find and cleanse the title of the report
    report_title = validate_title(params.get("report_title") or report_name, issues)
    # Get mailto email address
    mailto = validate_mailto(params.get("mailto"), issues)
    error_mailto = validate_mailto(params.get("error_mailto"), issues)
    mailfrom = validate_mailto(params.get("mailfrom"), issues)
    # "on" comes from HTML, "True" comes from urlencoded JSON params
    generate_pdf_output = params.get("generate_pdf") in ("on", "True", True)
    hide_code = params.get("hide_code") in ("on", "True", True)
    is_slideshow = params.get("is_slideshow") in ("on", "True", True)
    email_subject = validate_title(params.get("email_subject") or "", issues)

    out = RunReportParams(
        report_title=report_title,
        mailto=mailto,
        error_mailto=error_mailto,
        mailfrom=mailfrom,
        generate_pdf_output=generate_pdf_output,
        hide_code=hide_code,
        scheduler_job_id=params.get("scheduler_job_id"),
        is_slideshow=is_slideshow,
        email_subject=email_subject,
    )
    logger.info(f"Validated params: {out}")
    return out


def _handle_run_report(
    report_name: str, overrides_dict: Dict[str, Any], issues: List[str]
) -> Tuple[str, int, Dict[str, str]]:
    params = validate_run_params(report_name, request.values, issues)
    if issues:
        return jsonify({"status": "Failed", "content": ("\n".join(issues))})
    report_name = convert_report_name_url_to_path(report_name)
    logger.info(
        f"Handling run report with parameters report_name={report_name} "
        f"report_title={params.report_title}"
        f"mailto={params.mailto} "
        f"error_mailto={params.error_mailto} "
        f"overrides_dict={overrides_dict} "
        f"generate_pdf_output={params.generate_pdf_output} "
        f"hide_code={params.hide_code} "
        f"scheduler_job_id={params.scheduler_job_id} "
        f"mailfrom={params.mailfrom} "
        f"email_subject={params.email_subject} "
        f"is_slideshow={params.is_slideshow} "
    )
    try:
        with current_app.app_context():
            app_config = WebappConfig.from_superset_kwargs(current_app.config)
            job_id = run_report_in_subprocess(
                base_config=app_config,
                report_name=report_name,
                report_title=params.report_title,
                mailto=params.mailto,
                error_mailto=params.error_mailto,
                overrides=overrides_dict,
                generate_pdf_output=params.generate_pdf_output,
                hide_code=params.hide_code,
                scheduler_job_id=params.scheduler_job_id,
                mailfrom=params.mailfrom,
                email_subject=params.email_subject,
                is_slideshow=params.is_slideshow,
            )
            return (
                jsonify({"id": job_id}),
                202,  # HTTP Accepted code
                {"Location": url_for("pending_results_bp.task_status", report_name=report_name, job_id=job_id)},
            )
    except RuntimeError as e:
        return jsonify({"status": "Failed", "content": f"The job failed to initialise. Error: {str(e)}"}), 500, {}


@run_report_bp.route("/run_report_json/<path:report_name>", methods=["POST"])
def run_report_json(report_name):
    """
    Execute a notebook from a JSON request.

    :param report_name: The parameter here should be a "/"-delimited string which mirrors the directory structure of \
        the notebook templates.

    :returns: 202-redirects to the "task_status" interface.
    """
    issues = []
    # Get JSON overrides
    overrides_dict = json.loads(request.values.get("overrides", "{}"))
    return _handle_run_report(report_name, overrides_dict, issues)


@run_report_bp.route("/run_report/<path:report_name>", methods=["POST"])
def run_checks_http(report_name):
    """
    Execute a notebook from an HTTP request.

    :param report_name: The parameter here should be a "/"-delimited string which mirrors the directory structure of \
        the notebook templates.

    :returns: 202-redirects to the "task_status" interface.
    """
    issues = []
    # Get and process raw python overrides
    overrides_dict = handle_overrides(request.values.get("overrides"), issues)
    return _handle_run_report(report_name, overrides_dict, issues)


def _rerun_report(job_id, prepare_only=False, run_synchronously=False):
    result = get_serializer().get_check_result(job_id)
    if not result:
        abort(404)
    prefix = "Rerun of "
    title = result.report_title if result.report_title.startswith(prefix) else (prefix + result.report_title)

    with current_app.app_context():
        app_config = WebappConfig.from_superset_kwargs(current_app.config)
    new_job_id = run_report_in_subprocess(
        app_config,
        result.report_name,
        title,
        result.mailto,
        result.error_mailto,
        result.overrides,
        hide_code=result.hide_code,
        generate_pdf_output=result.generate_pdf_output,
        prepare_only=prepare_only,
        scheduler_job_id=None,  # the scheduler will never call rerun
        run_synchronously=run_synchronously,
        is_slideshow=result.is_slideshow,
        email_subject=result.email_subject,
        mailfrom=result.mailfrom,
    )
    return new_job_id


@run_report_bp.route("/rerun_report/<job_id>/<path:report_name>", methods=["POST"])
def rerun_report(job_id, report_name):
    """
    Rerun a notebook using its already-existing parameters.

    :param job_id: The Job ID of the report which we are rerunning.
    :param report_name: The parameter here should be a "/"-delimited string which mirrors the directory structure of \
        the notebook templates.

    :returns: 202-redirects to the "task_status" interface.
    """
    new_job_id = _rerun_report(job_id)
    return jsonify(
        {"results_url": url_for("serve_results_bp.task_results", report_name=report_name, job_id=new_job_id)}
    )


@run_report_bp.route("/delete_report/<job_id>", methods=["POST"])
def delete_report(job_id):
    """
    Deletes a report from the underlying storage. Only marks as "status=deleted" so the report is retrievable \
    at a later date.

    :param job_id: The UUID of the report to delete.

    :return: A JSON which contains "status" which will either be "ok" or "error".
    """
    try:
        get_serializer().delete_result(job_id)
        get_all_result_keys(get_serializer(), limit=DEFAULT_RESULT_LIMIT, force_reload=True)
        return jsonify({"status": "ok"}), 200
    except Exception:
        error_info = traceback.format_exc()
        return jsonify({"status": "error", "error": error_info}), 500


@run_report_bp.route("/delete_all_reports/<path:report_name>", methods=["POST"])
def delete_all_reports(report_name):
    """
    Deletes all reports associated with a particular report_name from the underlying storage.
    Only marks as "status=deleted" so the report is retrievable at a later date.

    :param report_name: The parameter here should be a "/"-delimited string which mirrors the directory structure of \
        the notebook templates.

    :return: A JSON which contains "status" which will either be "ok" or "error".
    """
    try:
        get_serializer().delete_many({"report_name": report_name})
        get_all_result_keys(get_serializer(), limit=DEFAULT_RESULT_LIMIT, force_reload=True)
        return jsonify({"status": "ok"}), 200
    except Exception:
        error_info = traceback.format_exc()
        return jsonify({"status": "error", "error": error_info}), 500