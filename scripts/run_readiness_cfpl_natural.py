"""Additional frozen ordinary-language CFPL pagination checks, same aggregate cap."""

from run_readiness_natural import main

CASES = [
    (
        "cfpl_worst_first",
        4,
        "请查询批次scat20161015_20161021整批已有物化CFPL误差中水平误差最大的样本，所有冻结时域，第一页2条，保留米和航班覆盖说明；不要调用GBAR或刷新物化。",
        "get_worst_prediction_cases",
        {"metric": "horizontal", "limit": 2, "offset": 0},
    ),
    (
        "cfpl_worst_next",
        4,
        "继续下一页相同口径的最差CFPL样本，仍然2条。",
        "get_worst_prediction_cases",
        {"metric": "horizontal", "limit": 2, "offset": 2},
    ),
    (
        "complex_filters",
        5,
        "查询批次scat20161015_20161021：呼号以SAS开头、机型为A320或B738、轨迹至少200点且已有SCAT预测，排除编号100002；这些条件须同时成立，最多显示2条。没有匹配就明确为空，不放宽条件。不要生成预测。",
        "search_scat_flights",
        {
            "callsign_prefix": "SAS",
            "aircraft_types": ["A320", "B738"],
            "min_track_points": 200,
            "has_scat_prediction": True,
            "exclude_external_ids": ["100002"],
            "limit": 2,
        },
    ),
]

if __name__ == "__main__":
    main(CASES, "cfpl-worst")
