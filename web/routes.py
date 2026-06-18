#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Web API路由

作者: 猫娘幽浮喵
"""

import datetime
import glob
import logging
import os
import time
import uuid
import zipfile
from typing import Optional

from flask import Flask, render_template, request, jsonify, send_file, abort

from src.config_manager import ConfigManager
from src.template_manager import TemplateManager
from src.core.models import GenerationConfig, GenerationResult
from src.core.track_analyzer import TrackAnalyzer

logger = logging.getLogger(__name__)

# 全局状态
_config_manager: Optional[ConfigManager] = None
_template_manager: Optional[TemplateManager] = None
_generation_jobs: dict = {}  # job_id -> {"results": [...], "zip_path": "..."}
_tracks_cache: Optional[list[dict]] = None  # 轨迹分析缓存

# ZIP 文件过期时间（秒），超过此时间的 ZIP 会被清理
ZIP_EXPIRE_SECONDS = 3600  # 1 小时


def _ensure_tracks_cache() -> list[dict]:
    """确保轨迹缓存已初始化（延迟初始化模式）

    Returns:
        缓存的轨迹列表
    """
    global _tracks_cache
    if _tracks_cache is None:
        _init_tracks_cache()
    return _tracks_cache


def _init_tracks_cache() -> None:
    """初始化轨迹缓存 - 预计算所有轨迹的分析结果"""
    global _tracks_cache
    if _config_manager is None:
        logger.warning("配置管理器未初始化，跳过缓存初始化")
        return
    _tracks_cache = []
    for track_id in _config_manager.list_tracks():
        try:
            track = _config_manager.load_track(track_id)
            analyzer = TrackAnalyzer(track.base_coordinates)
            analysis = analyzer.analyze_track()
            _tracks_cache.append({
                "id": track.id,
                "name": track.name,
                "description": track.description,
                "distance_meters": round(analysis.total_distance_meters, 1),
                "lap_distance_km": round(analysis.total_distance_meters / 1000, 3),
                "num_points": analysis.num_points,
                "is_clockwise": analysis.is_clockwise,
            })
        except Exception as e:
            logger.error("缓存轨迹 %s 失败: %s", track_id, e)
    logger.info("轨迹缓存初始化完成，共 %d 条", len(_tracks_cache))


def _create_zip_for_job(job_id: str, results: list) -> str:
    """在生成完成后立即创建 ZIP 压缩包

    在生成 API 中同步调用，避免下载时再压缩导致超时。
    使用 ZIP_STORED（不压缩）以加快打包速度，TCX 文本文件本身不大。

    Args:
        job_id: 任务 ID
        results: 生成结果列表

    Returns:
        ZIP 文件路径
    """
    output_dir = os.path.dirname(results[0].filepath)
    zip_path = os.path.join(output_dir, f"{job_id}.zip")

    # 使用 ZIP_STORED 不压缩，大幅加速打包
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED) as zf:
        for r in results:
            zf.write(r.filepath, os.path.basename(r.filepath))

    logger.info("ZIP 已创建: %s (%d 个文件)", zip_path, len(results))
    return zip_path


def _cleanup_old_zips(output_dir: str = "output") -> None:
    """清理过期的 ZIP 文件，防止磁盘堆积

    每次生成新 ZIP 时调用，清理超过 ZIP_EXPIRE_SECONDS 的旧 ZIP。

    Args:
        output_dir: 输出目录路径
    """
    now = time.time()
    pattern = os.path.join(output_dir, "gen_*.zip")
    for zip_path in glob.glob(pattern):
        try:
            age = now - os.path.getmtime(zip_path)
            if age > ZIP_EXPIRE_SECONDS:
                os.remove(zip_path)
                logger.info("已清理过期 ZIP: %s (%.0f 分钟前)", zip_path, age / 60)
        except OSError as e:
            logger.warning("清理 ZIP 失败 %s: %s", zip_path, e)


def create_app() -> Flask:
    """创建并配置 Flask 应用

    Returns:
        配置好的 Flask 应用实例
    """
    global _config_manager, _template_manager

    # 计算项目根目录（app.py 所在目录）
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    template_folder = os.path.join(project_root, "web", "templates")
    static_folder = os.path.join(project_root, "web", "static")

    app = Flask(
        __name__,
        template_folder=template_folder,
        static_folder=static_folder,
    )

    _config_manager = ConfigManager(os.path.join(project_root, "config"))
    _template_manager = TemplateManager(_config_manager)

    # 注册路由
    app.add_url_rule("/", "index", index)
    app.add_url_rule("/api/tracks", "list_tracks", list_tracks, methods=["GET"])
    app.add_url_rule(
        "/api/tracks/<track_id>", "get_track", get_track, methods=["GET"]
    )
    app.add_url_rule(
        "/api/templates", "list_templates", list_templates, methods=["GET"]
    )
    app.add_url_rule(
        "/api/template/<template_id>", "get_template", get_template, methods=["GET"]
    )
    app.add_url_rule(
        "/api/template", "create_template", create_template, methods=["POST"]
    )
    app.add_url_rule("/api/defaults", "get_defaults", get_defaults, methods=["GET"])
    app.add_url_rule(
        "/api/generate/daily", "generate_daily", generate_daily, methods=["POST"]
    )
    app.add_url_rule(
        "/api/generate/total", "generate_total", generate_total, methods=["POST"]
    )
    app.add_url_rule(
        "/api/generate/single", "generate_single", generate_single, methods=["POST"]
    )
    app.add_url_rule(
        "/api/generate/dates", "generate_dates", generate_dates, methods=["POST"]
    )
    app.add_url_rule(
        "/api/download/<job_id>", "download_files", download_files, methods=["GET"]
    )

    return app


def index():
    """渲染主页面"""
    return render_template("index.html")


def list_tracks():
    """列出所有可用轨迹（使用缓存）"""
    return jsonify(_ensure_tracks_cache())


def get_track(track_id):
    """获取轨迹详情"""
    try:
        track = _config_manager.load_track(track_id)
        return jsonify({
            "id": track.id,
            "name": track.name,
            "description": track.description,
            "num_points": len(track.base_coordinates),
            "has_correction": track.coordinate_correction is not None,
        })
    except FileNotFoundError:
        abort(404, description=f"轨迹 {track_id} 不存在")


def list_templates():
    """列出所有模板"""
    templates = _template_manager.list_available()
    return jsonify(templates)


def get_template(template_id):
    """获取模板详情（包含generation_config）"""
    template = _template_manager.load_template(template_id)
    if not template:
        abort(404, description=f"模板 {template_id} 不存在")
    return jsonify(template)


def create_template():
    """创建新模板"""
    data = request.get_json()
    if not data:
        return jsonify({"error": "无效的请求数据"}), 400

    name = data.get("name")
    description = data.get("description", "")
    generation_config = data.get("generation_config", {})

    if not name:
        return jsonify({"error": "模板名称不能为空"}), 400

    try:
        result = _template_manager.save_template(name, description, generation_config)
        return jsonify(result), 201
    except Exception as e:
        logger.error("创建模板失败: %s", e, exc_info=True)
        return jsonify({"error": str(e)}), 500


def get_defaults():
    """获取默认设置"""
    defaults = _config_manager.load_defaults()
    return jsonify(defaults)


def _parse_generate_request(data: dict) -> GenerationConfig:
    """从请求体解析生成配置

    Args:
        data: 请求体字典

    Returns:
        生成配置对象
    """
    overrides = {
        "min_pace": data.get("min_pace", 7.0),
        "max_pace": data.get("max_pace", 8.0),
        "start_time_min": data.get("start_time_min", "06:00"),
        "start_time_max": data.get("start_time_max", "08:00"),
        "output_dir": data.get("output_dir", "output"),
        "include_track": data.get("include_track", True),
        "apply_correction": data.get("apply_correction", True),
        "enable_pace_fluctuation": data.get("enable_pace_fluctuation", True),
    }
    if "track_id" in data:
        overrides["track_id"] = data["track_id"]

    return _template_manager.apply_template(
        template_id=data.get("template_id"),
        overrides=overrides,
    )


def generate_daily():
    """每日范围生成"""
    data = request.get_json()
    if not data:
        return jsonify({"error": "无效的请求数据"}), 400

    try:
        config = _parse_generate_request(data)
        start = datetime.datetime.strptime(data["start_date"], "%Y-%m-%d").date()
        end = datetime.datetime.strptime(data["end_date"], "%Y-%m-%d").date()
        min_km = float(data["min_km"])
        max_km = float(data["max_km"])

        from src.generation_engine import GenerationEngine

        engine = GenerationEngine(_config_manager)
        results = engine.generate_daily(start, end, min_km, max_km, config)

        job_id = (
            f"gen_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}"
            f"_{uuid.uuid4().hex[:6]}"
        )

        # 清理过期 ZIP，然后立即创建 ZIP（避免下载时超时）
        output_dir = os.path.abspath(config.output_dir)
        _cleanup_old_zips(output_dir)
        zip_path = _create_zip_for_job(job_id, results)

        _generation_jobs[job_id] = {"results": results, "zip_path": zip_path}

        return jsonify({
            "job_id": job_id,
            "status": "complete",
            "total_files": len(results),
            "files": [_result_to_dict(r) for r in results],
            "download_url": f"/api/download/{job_id}",
        })
    except Exception as e:
        logger.error("生成失败: %s", e, exc_info=True)
        return jsonify({"error": str(e)}), 500


def generate_total():
    """总公里数生成"""
    data = request.get_json()
    if not data:
        return jsonify({"error": "无效的请求数据"}), 400

    try:
        config = _parse_generate_request(data)
        config.weekend_factor = data.get("weekend_factor", config.weekend_factor)
        config.rest_days_per_week = data.get(
            "rest_days_per_week", config.rest_days_per_week
        )
        config.min_daily_km = data.get("min_daily_km", config.min_daily_km)
        config.max_daily_km = data.get("max_daily_km", config.max_daily_km)

        start = datetime.datetime.strptime(data["start_date"], "%Y-%m-%d").date()
        end = datetime.datetime.strptime(data["end_date"], "%Y-%m-%d").date()
        total_km = float(data["total_km"])

        from src.generation_engine import GenerationEngine

        engine = GenerationEngine(_config_manager)
        results = engine.generate_total(start, end, total_km, config)

        job_id = (
            f"gen_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}"
            f"_{uuid.uuid4().hex[:6]}"
        )

        # 清理过期 ZIP，然后立即创建 ZIP（避免下载时超时）
        output_dir = os.path.abspath(config.output_dir)
        _cleanup_old_zips(output_dir)
        zip_path = _create_zip_for_job(job_id, results)

        _generation_jobs[job_id] = {"results": results, "zip_path": zip_path}

        return jsonify({
            "job_id": job_id,
            "status": "complete",
            "total_files": len(results),
            "files": [_result_to_dict(r) for r in results],
            "download_url": f"/api/download/{job_id}",
        })
    except Exception as e:
        logger.error("生成失败: %s", e, exc_info=True)
        return jsonify({"error": str(e)}), 500


def generate_single():
    """单文件生成"""
    data = request.get_json()
    if not data:
        return jsonify({"error": "无效的请求数据"}), 400

    try:
        config = _parse_generate_request(data)
        date = datetime.datetime.strptime(data["date"], "%Y-%m-%d").date()
        distance = float(data["distance"])

        from src.generation_engine import GenerationEngine

        engine = GenerationEngine(_config_manager)
        result = engine.generate_single(date, distance, config)

        job_id = (
            f"gen_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}"
            f"_{uuid.uuid4().hex[:6]}"
        )
        _generation_jobs[job_id] = {"results": [result], "zip_path": None}

        return jsonify({
            "job_id": job_id,
            "status": "complete",
            "total_files": 1,
            "files": [_result_to_dict(result)],
            "download_url": f"/api/download/{job_id}",
        })
    except Exception as e:
        logger.error("生成失败: %s", e, exc_info=True)
        return jsonify({"error": str(e)}), 500


def generate_dates():
    """指定日期生成"""
    data = request.get_json()
    if not data:
        return jsonify({"error": "无效的请求数据"}), 400

    try:
        config = _parse_generate_request(data)
        dates_list = data.get("dates", [])
        if not dates_list:
            return jsonify({"error": "请至少选择一个日期"}), 400

        min_km = float(data.get("min_km", 2.0))
        max_km = float(data.get("max_km", 5.0))

        from src.generation_engine import GenerationEngine

        engine = GenerationEngine(_config_manager)

        # 为每个选定日期生成 DailyPlan
        import random
        from src.core.models import DailyPlan

        plans = []
        for date_str in dates_list:
            date = datetime.datetime.strptime(date_str, "%Y-%m-%d").date()
            distance = round(random.uniform(min_km, max_km), 2)
            plans.append(DailyPlan(date=date, distance_km=distance))

        results = engine._generate_from_plans(plans, config)

        job_id = (
            f"gen_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}"
            f"_{uuid.uuid4().hex[:6]}"
        )

        # 清理过期 ZIP，然后立即创建 ZIP
        output_dir = os.path.abspath(config.output_dir)
        _cleanup_old_zips(output_dir)
        zip_path = _create_zip_for_job(job_id, results)

        _generation_jobs[job_id] = {"results": results, "zip_path": zip_path}

        return jsonify({
            "job_id": job_id,
            "status": "complete",
            "total_files": len(results),
            "files": [_result_to_dict(r) for r in results],
            "download_url": f"/api/download/{job_id}",
        })
    except Exception as e:
        logger.error("生成失败: %s", e, exc_info=True)
        return jsonify({"error": str(e)}), 500


def download_files(job_id):
    """下载生成的文件（ZIP 已在生成时创建，此处直接发送）"""
    if job_id not in _generation_jobs:
        return jsonify({"error": "任务不存在或已过期"}), 404

    job = _generation_jobs[job_id]
    results = job["results"]
    zip_path = job.get("zip_path")

    if len(results) == 1:
        # 单文件：直接发送 TCX 文件
        return send_file(
            results[0].filepath,
            as_attachment=True,
            download_name=os.path.basename(results[0].filepath),
        )

    # 多文件：发送预创建的 ZIP（生成时已打包，避免下载时超时）
    if zip_path and os.path.isfile(zip_path):
        return send_file(
            zip_path,
            as_attachment=True,
            download_name=f"{job_id}.zip",
            mimetype="application/zip",
        )

    # 兜底：如果 ZIP 文件丢失，尝试重新创建
    logger.warning("ZIP 文件丢失，重新创建: %s", zip_path)
    output_dir = os.path.dirname(results[0].filepath)
    zip_path = os.path.join(output_dir, f"{job_id}.zip")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED) as zf:
        for r in results:
            zf.write(r.filepath, os.path.basename(r.filepath))
    job["zip_path"] = zip_path
    return send_file(
        zip_path,
        as_attachment=True,
        download_name=f"{job_id}.zip",
        mimetype="application/zip",
    )


def _result_to_dict(result: GenerationResult) -> dict:
    """将生成结果转换为字典

    Args:
        result: 生成结果对象

    Returns:
        结果字典
    """
    return {
        "filename": os.path.basename(result.filepath),
        "date": result.date.strftime("%Y-%m-%d"),
        "distance_km": result.distance_km,
        "pace_min_per_km": result.pace_min_per_km,
        "duration_seconds": result.duration_seconds,
        "calories": result.calories,
    }
