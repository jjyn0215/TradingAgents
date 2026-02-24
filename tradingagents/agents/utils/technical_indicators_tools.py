from langchain_core.tools import tool
from typing import Annotated
from tradingagents.dataflows.interface import route_to_vendor


def _normalize_indicator_input(indicator) -> list[str]:
    """Normalize indicator input into a unique list of indicator names."""
    if isinstance(indicator, str):
        raw_parts = indicator.replace("\n", ",").split(",")
    elif isinstance(indicator, (list, tuple, set)):
        raw_parts = [str(item) for item in indicator]
    else:
        raw_parts = [str(indicator)]

    normalized = []
    for part in raw_parts:
        item = part.strip()
        if item and item not in normalized:
            normalized.append(item)

    if not normalized:
        raise ValueError("indicator is empty")

    return normalized


@tool
def get_indicators(
    symbol: Annotated[str, "ticker symbol of the company"],
    indicator: Annotated[str, "technical indicator to get the analysis and report of"],
    curr_date: Annotated[str, "The current trading date you are trading on, YYYY-mm-dd"],
    look_back_days: Annotated[int, "how many days to look back"] = 30,
) -> str:
    """
    Retrieve technical indicators for a given ticker symbol.
    Uses the configured technical_indicators vendor.
    Args:
        symbol (str): Ticker symbol of the company, e.g. AAPL, TSM
        indicator (str): Technical indicator to get the analysis and report of
        curr_date (str): The current trading date you are trading on, YYYY-mm-dd
        look_back_days (int): How many days to look back, default is 30
    Returns:
        str: A formatted dataframe containing the technical indicators for the specified ticker symbol and indicator.
    """
    indicators = _normalize_indicator_input(indicator)

    if len(indicators) == 1:
        return route_to_vendor(
            "get_indicators", symbol, indicators[0], curr_date, look_back_days
        )

    reports = []
    errors = []
    for item in indicators:
        try:
            report = route_to_vendor(
                "get_indicators", symbol, item, curr_date, look_back_days
            )
            reports.append(report)
        except Exception as exc:
            errors.append(f"- {item}: {exc}")

    if not reports:
        raise ValueError(
            "No indicator report generated. Requested indicators:\n"
            + "\n".join(errors)
        )

    if errors:
        reports.append(
            "## Indicator warnings\n"
            "Some indicators could not be calculated:\n"
            + "\n".join(errors)
        )

    return "\n\n".join(reports)
