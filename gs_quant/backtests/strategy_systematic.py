"""
Copyright 2019 Goldman Sachs.
Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

  http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing,
software distributed under the License is distributed on an
"AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
KIND, either express or implied.  See the License for the
specific language governing permissions and limitations
under the License.
"""
import logging
from typing import Iterable

import gs_quant.target.backtests as backtests
from gs_quant.api.gs.backtests import GsBacktestApi
from gs_quant.api.gs.backtests_xasset.apis import GsBacktestXassetApi
from gs_quant.api.gs.backtests_xasset.request import BasicBacktestRequest
from gs_quant.api.gs.backtests_xasset.response_datatypes.backtest_datatypes import DateConfig, Trade, \
    CostPerTransaction, TransactionCostModel
from gs_quant.backtests.core import Backtest, TradeInMethod
from gs_quant.errors import MqValueError
from gs_quant.target.backtests import *
from gs_quant.instrument import EqOption, EqVarianceSwap, Instrument
from gs_quant.target.instrument import FXOption, FXBinary

_logger = logging.getLogger(__name__)

BACKTEST_TYPE_NAME = 'VolatilityFlow'
BACKTEST_TYPE_VALUE = 'Volatility Flow'
ISO_FORMAT = r"^([0-9]{4})-([0-9]{2})-([0-9]{2})$"


class StrategySystematic:
    """Equity back testing systematic strategy"""
    _supported_eq_instruments = (EqOption, EqVarianceSwap)
    _supported_fx_instruments = (FXOption, FXBinary)
    _supported_instruments = _supported_eq_instruments + _supported_fx_instruments

    def __init__(self,
                 underliers: Union[Instrument, Iterable[Instrument]],
                 quantity: float = 1,
                 quantity_type: Union[BacktestTradingQuantityType, str] = BacktestTradingQuantityType.notional,
                 trade_in_method: Union[TradeInMethod, str] = TradeInMethod.FixedRoll,
                 roll_frequency: str = None,
                 scaling_method: str = None,
                 index_initial_value: float = 0.0,
                 delta_hedge: DeltaHedgeParameters = None,
                 name: str = None,
                 cost_netting: bool = False,
                 currency: Union[Currency, str] = Currency.USD,
                 trade_in_signals: Tuple[BacktestSignalSeriesItem, ...] = None,
                 trade_out_signals: Tuple[BacktestSignalSeriesItem, ...] = None,
                 market_model: Union[EquityMarketModel, str] = EquityMarketModel.SFK,
                 roll_date_mode: str = None,
                 expiry_date_mode: str = None,
                 cash_accrual: bool = True):
        self.__cost_netting = cost_netting
        self.__currency = get_enum_value(Currency, currency)
        self.__name = name
        self.__backtest_type = BACKTEST_TYPE_NAME
        self.__cash_accrual = cash_accrual

        trade_in_method = get_enum_value(TradeInMethod, trade_in_method).value
        market_model = get_enum_value(EquityMarketModel, market_model).value

        self.__trading_parameters = BacktestTradingParameters(
            quantity=quantity,
            quantity_type=get_enum_value(BacktestTradingQuantityType, quantity_type).value,
            trade_in_method=trade_in_method,
            roll_frequency=roll_frequency,
            trade_in_signals=trade_in_signals,
            trade_out_signals=trade_out_signals,
            roll_date_mode=roll_date_mode
        )

        self.__underliers = []
        trade_instruments = []
        if isinstance(underliers, self._supported_instruments):
            instrument = underliers
            notional_percentage = 100
            instrument = self.check_underlier_fields(instrument)
            trade_instruments.append(instrument)
            self.__underliers.append(BacktestStrategyUnderlier(
                instrument=instrument,
                notional_percentage=notional_percentage,
                hedge=BacktestStrategyUnderlierHedge(risk_details=delta_hedge),
                market_model=market_model,
                expiry_date_mode=expiry_date_mode))
        else:
            for underlier in underliers:
                if isinstance(underlier, tuple):
                    instrument = underlier[0]
                    notional_percentage = underlier[1]
                else:
                    instrument = underlier
                    notional_percentage = 100

                if not isinstance(instrument, self._supported_instruments):
                    raise MqValueError('The format of the backtest asset is incorrect.')
                elif isinstance(instrument, self._supported_fx_instruments):
                    instrument.notional_amount *= notional_percentage / 100

                instrument = self.check_underlier_fields(instrument)
                trade_instruments.append(instrument)
                self.__underliers.append(BacktestStrategyUnderlier(
                    instrument=instrument,
                    notional_percentage=notional_percentage,
                    hedge=BacktestStrategyUnderlierHedge(risk_details=delta_hedge),
                    market_model=market_model,
                    expiry_date_mode=expiry_date_mode))
        # xasset backtesting service fields
        self.__trades = (Trade(tuple(trade_instruments), roll_frequency, roll_frequency, quantity, quantity_type),)
        self.__delta_hedge_frequency = '1b' if delta_hedge else None

        backtest_parameters_class: Base = getattr(backtests, self.__backtest_type + 'BacktestParameters')
        backtest_parameter_args = {
            'trading_parameters': self.__trading_parameters,
            'underliers': self.__underliers,
            'trade_in_method': trade_in_method,
            'scaling_method': scaling_method,
            'index_initial_value': index_initial_value
        }
        self.__backtest_parameters = backtest_parameters_class.from_dict(backtest_parameter_args)
        all_eq = all(isinstance(i, self._supported_eq_instruments) for i in trade_instruments)
        all_fx = all(isinstance(i, self._supported_fx_instruments) for i in trade_instruments)
        if not (all_eq or all_fx):
            raise MqValueError('Cannot run backtests for different asset classes.')
        self.__use_xasset_backtesting_service = all_fx

    @staticmethod
    def check_underlier_fields(
            underlier: Instrument
    ) -> Instrument:

        if isinstance(underlier, EqOption):
            underlier.number_of_options = None

        return underlier

    def __run_service_based_backtest(self, start: datetime.date, end: datetime.date,
                                     measures: Iterable[FlowVolBacktestMeasure]) -> BacktestResult:
        date_cfg = DateConfig(start, end)
        calc_measures = tuple(m for m in measures if m != FlowVolBacktestMeasure.portfolio)
        if not calc_measures:
            calc_measures = (FlowVolBacktestMeasure.PNL,)
        basic_bt_request = BasicBacktestRequest(date_cfg, self.__trades, calc_measures, self.__delta_hedge_frequency,
                                                CostPerTransaction(TransactionCostModel.Fixed, 0), None)
        basic_bt_response = GsBacktestXassetApi.calculate_basic_backtest(basic_bt_request)
        risks = tuple(
            BacktestRisk(name=k.value,
                         timeseries=tuple(FieldValueMap(date=d, value=r.result) for d, r in v.items()))
            for k, v in basic_bt_response.measures.items()
        )
        portfolio = []
        if FlowVolBacktestMeasure.portfolio in measures:
            for d in sorted(set().union(basic_bt_response.portfolio.keys(), basic_bt_response.transactions.keys())):
                if d in basic_bt_response.portfolio:
                    positions = [{'instrument': i.to_dict() if i is not None else {}} for
                                 i in basic_bt_response.portfolio[d]]
                else:
                    positions = []
                transactions = []
                if d in basic_bt_response.transactions:
                    for t in basic_bt_response.transactions[d]:
                        trades = [{'instrument': i.to_dict() if i is not None else {},
                                   'price': t.portfolio_price}
                                  for i in t.portfolio] if t.portfolio is not None else []
                        transactions.append({'type': t.direction.value, 'trades': trades})
                portfolio.append({'date': d, 'positions': positions, 'transactions': transactions})
        return BacktestResult(risks=risks, portfolio=portfolio)

    def backtest(
            self,
            start: datetime.date = None,
            end: datetime.date = datetime.date.today() - datetime.timedelta(days=1),
            is_async: bool = False,
            measures: Iterable[FlowVolBacktestMeasure] = (FlowVolBacktestMeasure.ALL_MEASURES,),
            correlation_id: str = None
    ) -> Union[Backtest, BacktestResult]:
        if self.__use_xasset_backtesting_service:
            return self.__run_service_based_backtest(start, end, measures)
        params_dict = self.__backtest_parameters.as_dict()
        params_dict['measures'] = [m.value for m in measures]
        backtest_parameters_class: Base = getattr(backtests, self.__backtest_type + 'BacktestParameters')
        params = backtest_parameters_class.from_dict(params_dict)

        backtest = Backtest(name=self.__name,
                            mq_symbol=self.__name,
                            parameters=params,
                            start_date=start,
                            end_date=end,
                            type=BACKTEST_TYPE_VALUE,
                            asset_class=AssetClass.Equity,
                            currency=self.__currency,
                            cost_netting=self.__cost_netting,
                            cash_accrual=self.__cash_accrual)

        if is_async:
            # Create back test ...
            response = GsBacktestApi.create_backtest(backtest)

            # ... and schedule it
            GsBacktestApi.schedule_backtest(backtest_id=response.id)
        else:
            # Run on-the-fly back test
            response = GsBacktestApi.run_backtest(backtest, correlation_id)

        return response
