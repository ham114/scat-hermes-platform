---
name: s7b-flight-investigation
description: 受限脚本演示航班查询、质量与轨迹只读编排，实际工具引用可回查。
---

## 适用条件

有明确批次/呼号的只读调查；不执行预测或数据库写入。

## 必要参数

batch、callsign、search_limit、track_limit必须由用户目标或实际工具结果提供。读取scripts/investigate.json获取类型和范围；不猜标识。

## 输入输出

输入scat.request.v1；脚本输出航班/点数、高度米、缺失点及真实external_ids。空/错误明确标注，不把默认0当有效统计。

## 前置条件

先加载本技能，再分别加载scat-flight-query、scat-data-quality、scat-trajectory-analysis；依赖不继承权限。

## 允许操作

通过已加载子技能使用三个登记的只读操作，脚本本身没有额外权限。

## 执行流程

读取脚本描述后调用skill_script_run(skill=s7b-flight-investigation,script=scripts/investigate.py,inputs=实际参数,request_id=稳定UUID)。脚本查询→有界整理标识→空结果停止→质量→轨迹→整理；依据真实回执答复。

## 完成条件

实际执行脚本并核对business_result_references，状态ok且出处、范围、单位一致；合成数据必须标明合成。

## 失败处理

缺参先补问。empty/error/unknown均不得宣称完成；unknown不换UUID重放；权限拒绝时检查实际技能加载。

## 示例

合成留出条件由测试平台给出。查询1个航班、读2点时传search_limit=1、track_limit=2，不把示例标识用于真实业务。

## 评测

三项脚本合成用例（成功、空、错误），及独立原生留出三次；不能用作者用例替代可信原生证据。
