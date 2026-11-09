# 52项旧工具语义审查索引（2026-11-09）

此表按原迁移矩阵逐项保留52行，提供核验入口；不是52项自然语言或旧站交互全部通过的证明。当前上线结论见 [上线审查](RELEASE_READINESS_REVIEW_20261109.md)。

本轮真实自然语言记录：问题/合格两页、空后改查、CFPL汇总/最差两页、复杂过滤；任意as_of两CPU配置实验及单航班阶段物化/报告；ZYJ独立物化准备与HYN报告复用。原有SCAT/UAV自然语言和页面记录保留。合并到workflow的旧函数仍须按原参数/范围/取消/失败语义分别对照，不能从执行单元已测推导该行全部旧语义已通过。

| 旧工具 | 现有目标 | 登记状态 | 证据判断与边界 |
| --- | --- | --- | --- |
| get_scat_flight_phase_segments | scat_materialization_read(phase_segments) | scoped_merge | 相关受控执行单元有真实实验/GPU/报告证据；该旧函数的全部语义尚不能由此推出 |
| get_worst_prediction_cases | mcp__scat__get_worst_prediction_cases | native_readonly | 本轮有普通自然语言及真实参数回执；仍按记录的批次/分页/条件范围解释 |
| get_prediction_metric_summaries | mcp__scat__get_prediction_metric_summaries | native_readonly | 本轮有普通自然语言及真实参数回执；仍按记录的批次/分页/条件范围解释 |
| prepare_prediction_experiment | scat_workflow_prepare(kind=experiment) | scoped_merge | 相关受控执行单元有真实实验/GPU/报告证据；该旧函数的全部语义尚不能由此推出 |
| submit_prediction_experiment | POST /api/v1/hermes/threads/{thread}/model-tasks/{task}/confirm | http_control | 控制面/保留/合并项按原scope_note核对；不能当作新自然语言直接开放 |
| cancel_prediction_experiment_submission | POST /api/v1/hermes/threads/{thread}/model-tasks/{task}/revoke or /cancel | http_control | 控制面/保留/合并项按原scope_note核对；不能当作新自然语言直接开放 |
| get_experiment_run | scat_workflow_list / scat_workflow_get | scoped_merge | 相关受控执行单元有真实实验/GPU/报告证据；该旧函数的全部语义尚不能由此推出 |
| list_prediction_experiments | scat_workflow_list / scat_workflow_get | scoped_merge | 相关受控执行单元有真实实验/GPU/报告证据；该旧函数的全部语义尚不能由此推出 |
| prepare_materialization_job | scat_workflow_prepare(kind=materialization) | scoped_merge | 相关受控执行单元有真实实验/GPU/报告证据；该旧函数的全部语义尚不能由此推出 |
| submit_materialization_job | POST /api/v1/hermes/threads/{thread}/model-tasks/{task}/confirm | http_control | 控制面/保留/合并项按原scope_note核对；不能当作新自然语言直接开放 |
| cancel_materialization_job_submission | POST /api/v1/hermes/threads/{thread}/model-tasks/{task}/revoke or /cancel | http_control | 控制面/保留/合并项按原scope_note核对；不能当作新自然语言直接开放 |
| get_materialization_job | scat_workflow_list / scat_workflow_get | scoped_merge | 相关受控执行单元有真实实验/GPU/报告证据；该旧函数的全部语义尚不能由此推出 |
| list_materialization_jobs | scat_workflow_list / scat_workflow_get | scoped_merge | 相关受控执行单元有真实实验/GPU/报告证据；该旧函数的全部语义尚不能由此推出 |
| list_task_models | scat_task_catalog | native_control | 控制面/保留/合并项按原scope_note核对；不能当作新自然语言直接开放 |
| prepare_model_task | scat_task_prepare / scat_workflow_prepare | scoped_merge | 相关受控执行单元有真实实验/GPU/报告证据；该旧函数的全部语义尚不能由此推出 |
| prepare_random_gbar_batch | scat_workflow_prepare (selection=random, model=scat-gbar-v23:23) | scoped_merge | 相关受控执行单元有真实实验/GPU/报告证据；该旧函数的全部语义尚不能由此推出 |
| prepare_random_comparison_batch | scat_workflow_prepare(kind=comparison) | scoped_merge | 相关受控执行单元有真实实验/GPU/报告证据；该旧函数的全部语义尚不能由此推出 |
| prepare_comparison_batch | scat_workflow_prepare(kind=comparison) | scoped_merge | 相关受控执行单元有真实实验/GPU/报告证据；该旧函数的全部语义尚不能由此推出 |
| submit_model_task | POST /api/v1/hermes/threads/{thread}/model-tasks/{task}/confirm | http_control | 控制面/保留/合并项按原scope_note核对；不能当作新自然语言直接开放 |
| get_analysis_artifact | scat_task_artifact / scat_workflow_artifact | native_control | 相关受控执行单元有真实实验/GPU/报告证据；该旧函数的全部语义尚不能由此推出 |
| get_model_run | scat_task_get / scat_workflow_get | native_control | 相关受控执行单元有真实实验/GPU/报告证据；该旧函数的全部语义尚不能由此推出 |
| list_model_runs | scat_task_list / scat_workflow_list | native_control | 相关受控执行单元有真实实验/GPU/报告证据；该旧函数的全部语义尚不能由此推出 |
| list_analysis_artifacts | GET /api/v1/analysis/artifacts | retained_http | 控制面/保留/合并项按原scope_note核对；不能当作新自然语言直接开放 |
| cancel_model_task | POST /api/v1/hermes/threads/{thread}/model-tasks/{task}/revoke | http_control | 控制面/保留/合并项按原scope_note核对；不能当作新自然语言直接开放 |
| cancel_model_run | POST /api/v1/hermes/threads/{thread}/model-tasks/{task}/cancel | http_control | 控制面/保留/合并项按原scope_note核对；不能当作新自然语言直接开放 |
| list_prediction_models | scat_workflow_catalog | scoped_merge | 相关受控执行单元有真实实验/GPU/报告证据；该旧函数的全部语义尚不能由此推出 |
| predict_scat_flight_trajectory | scat_workflow_prepare + scat_trial_run | scoped_merge | 相关受控执行单元有真实实验/GPU/报告证据；该旧函数的全部语义尚不能由此推出 |
| search_scat_knowledge | mcp__scat__search_scat_knowledge | native_readonly | 查看前轮及本轮真实只读/页面记录；未单独将本行记为全语义通过 |
| get_scat_database_overview | mcp__scat__get_scat_database_overview | native_readonly | 查看前轮及本轮真实只读/页面记录；未单独将本行记为全语义通过 |
| list_scat_batches | mcp__scat__list_scat_batches | native_readonly | 查看前轮及本轮真实只读/页面记录；未单独将本行记为全语义通过 |
| inspect_scat_collection_data_quality | mcp__scat__inspect_scat_collection_data_quality | native_readonly | 查看前轮及本轮真实只读/页面记录；未单独将本行记为全语义通过 |
| list_scat_quality_problem_flights | mcp__scat__list_scat_quality_problem_flights | native_readonly | 本轮有普通自然语言及真实参数回执；仍按记录的批次/分页/条件范围解释 |
| list_scat_quality_clean_flights | mcp__scat__list_scat_quality_clean_flights | native_readonly | 本轮有普通自然语言及真实参数回执；仍按记录的批次/分页/条件范围解释 |
| get_scat_batch_overview | mcp__scat__get_scat_batch_overview | native_readonly | 查看前轮及本轮真实只读/页面记录；未单独将本行记为全语义通过 |
| search_scat_flights | mcp__scat__search_scat_flights | native_readonly | 本轮有普通自然语言及真实参数回执；仍按记录的批次/分页/条件范围解释 |
| get_scat_flight_overview | mcp__scat__get_scat_flight_overview | native_readonly | 查看前轮及本轮真实只读/页面记录；未单独将本行记为全语义通过 |
| get_scat_track_points | mcp__scat__get_scat_track_points | native_readonly | 查看前轮及本轮真实只读/页面记录；未单独将本行记为全语义通过 |
| inspect_scat_model_data_quality | mcp__scat__inspect_scat_model_data_quality | native_readonly | 查看前轮及本轮真实只读/页面记录；未单独将本行记为全语义通过 |
| get_scat_fpl_overview | mcp__scat__get_scat_fpl_overview | native_readonly | 查看前轮及本轮真实只读/页面记录；未单独将本行记为全语义通过 |
| get_scat_prediction_overview | mcp__scat__get_scat_prediction_overview | native_readonly | 查看前轮及本轮真实只读/页面记录；未单独将本行记为全语义通过 |
| get_scat_prediction_snapshot | mcp__scat__get_scat_prediction_snapshot | native_readonly | 查看前轮及本轮真实只读/页面记录；未单独将本行记为全语义通过 |
| get_scat_airport_stats | mcp__scat__get_scat_airport_stats | native_readonly | 查看前轮及本轮真实只读/页面记录；未单独将本行记为全语义通过 |
| get_scat_flight_phase_stats | mcp__scat__get_scat_flight_phase_stats | native_readonly | 查看前轮及本轮真实只读/页面记录；未单独将本行记为全语义通过 |
| get_scat_prediction_error_stats | mcp__scat__get_scat_prediction_error_stats | native_readonly | 查看前轮及本轮真实只读/页面记录；未单独将本行记为全语义通过 |
| list_uav_models | mcp__scat__list_uav_models | native_readonly | 查看前轮及本轮真实只读/页面记录；未单独将本行记为全语义通过 |
| search_uav_flights | mcp__scat__search_uav_flights | native_readonly | 查看前轮及本轮真实只读/页面记录；未单独将本行记为全语义通过 |
| get_uav_flight_overview | mcp__scat__get_uav_flight_overview | native_readonly | 查看前轮及本轮真实只读/页面记录；未单独将本行记为全语义通过 |
| list_uav_channels | mcp__scat__list_uav_channels | native_readonly | 查看前轮及本轮真实只读/页面记录；未单独将本行记为全语义通过 |
| get_uav_samples | mcp__scat__get_uav_samples | native_readonly | 相关受控执行单元有真实实验/GPU/报告证据；该旧函数的全部语义尚不能由此推出 |
| get_uav_channel_stats | mcp__scat__get_uav_channel_stats | native_readonly | 查看前轮及本轮真实只读/页面记录；未单独将本行记为全语义通过 |
| get_uav_normal_vs_anomaly_stats | mcp__scat__get_uav_normal_vs_anomaly_stats | native_readonly | 查看前轮及本轮真实只读/页面记录；未单独将本行记为全语义通过 |
| get_uav_phase_stats | mcp__scat__get_uav_phase_stats | native_readonly | 查看前轮及本轮真实只读/页面记录；未单独将本行记为全语义通过 |

每行的迁移理由与范围以 `services/hermes/legacy-tool-mapping.v1.json` 的scope_note为准。后续验收需为每行补原指令、实际参数、真实回执/制品、旧站交互和失败边界；当前不提供“52/52完成”或完成百分比。
