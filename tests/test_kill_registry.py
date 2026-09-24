"""Реестр колбэков kill-switch connector.py (задача KILL-CALLBACK-OWNER).

Проблема: risk.set_order_canceller хранит один слот — при одновременной
работе engine и OrderRouter последняя регистрация перезаписывала предыдущую.
Решение: реестр подписчиков в connector, в risk регистрируется единый
диспетчер kill_switch_dispatch.

Проверяет (критерий готовности задачи):
- регистрация из двух мест не теряет ни одного колбэка — при срабатывании
  вызываются оба;
- повторная регистрация не дублирует вызов (идемпотентность), включая
  bound-методы одного объекта;
- исключение одного подписчика не мешает остальным;
- интеграция с risk.kill_switch: диспетчер вызывает всех подписчиков;
- OrderRouter подписывается при __init__ и отписывается в close().
"""
import tempfile
import unittest
from pathlib import Path

from src import risk
from src.connector import (
    kill_switch_dispatch,
    register_kill_callback,
    registered_kill_callbacks,
    unregister_kill_callback,
)
from src.order_router import OrderRouter


class _Subscriber:
    """Фейк-подписчик kill-switch: считает вызовы, возвращает отчёт контракта."""

    def __init__(self, cancelled=()):
        self.calls: list[bool] = []
        self.report = {"cancelled": list(cancelled), "failed": [], "errors": []}

    def __call__(self, flatten: bool) -> dict:
        self.calls.append(flatten)
        return dict(self.report)


class _RegistryCase(unittest.TestCase):
    """База: реестр — модульный синглтон, откатываем изменения после теста."""

    def setUp(self):
        self._before = registered_kill_callbacks()

    def tearDown(self):
        for cb in registered_kill_callbacks():
            if cb not in self._before:
                unregister_kill_callback(cb)


class KillRegistryTest(_RegistryCase):
    """Реестр connector: подписка, идемпотентность, изоляция ошибок."""

    def test_two_subscribers_both_called(self):
        a, b = _Subscriber(cancelled=["o2"]), _Subscriber(cancelled=["o1"])
        register_kill_callback(a)
        register_kill_callback(b)
        report = kill_switch_dispatch(flatten=False)
        self.assertEqual(a.calls, [False])
        self.assertEqual(b.calls, [False])
        # отчёты сливаются: cancelled — объединение без дублей, отсортировано
        self.assertEqual(report["cancelled"], ["o1", "o2"])
        self.assertEqual(report["failed"], [])

    def test_reregistration_does_not_duplicate_call(self):
        a = _Subscriber()
        register_kill_callback(a)
        register_kill_callback(a)
        register_kill_callback(a)
        kill_switch_dispatch()
        self.assertEqual(len(a.calls), 1)

    def test_bound_method_registration_is_idempotent(self):
        # bound-методы одного объекта равны по ==: повторная регистрация
        # self._cancel_all_orders того же экземпляра роутера не дублирует вызов
        class Owner:
            def __init__(self):
                self.calls = 0

            def on_kill(self, flatten):
                self.calls += 1
                return {"cancelled": [], "failed": []}

        owner = Owner()
        register_kill_callback(owner.on_kill)
        register_kill_callback(owner.on_kill)  # новый bound-объект, та же (self, func)
        kill_switch_dispatch()
        self.assertEqual(owner.calls, 1)

    def test_unregister_is_noop_when_absent(self):
        a = _Subscriber()
        register_kill_callback(a)
        unregister_kill_callback(a)
        unregister_kill_callback(a)  # повторная отписка — no-op
        self.assertNotIn(a, registered_kill_callbacks())

    def test_subscriber_exception_does_not_block_others(self):
        def bad(flatten):
            raise RuntimeError("биржа недоступна")

        ok = _Subscriber(cancelled=["o9"])
        register_kill_callback(bad)
        register_kill_callback(ok)
        report = kill_switch_dispatch()
        self.assertEqual(ok.calls, [False])
        self.assertEqual(report["cancelled"], ["o9"])
        self.assertTrue(
            any("биржа недоступна" in str(f) for f in report["failed"]), str(report))

    def test_dispatch_without_subscribers_reports_failure(self):
        # пустой реестр — явный failed, а не молчаливый успех
        for cb in registered_kill_callbacks():
            unregister_kill_callback(cb)
        try:
            report = kill_switch_dispatch()
            self.assertEqual(report["cancelled"], [])
            self.assertTrue(report["failed"])
        finally:
            for cb in self._before:  # восстановить снимок для tearDown
                register_kill_callback(cb)


class KillRegistryRiskIntegrationTest(_RegistryCase):
    """Интеграция: risk.kill_switch -> диспетчер -> ВСЕ подписчики."""

    def setUp(self):
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        risk.init(Path(self._tmp.name) / "risk.db")  # свежий core: canceller=None

    def tearDown(self):
        super().tearDown()
        risk.init(Path(self._tmp.name) / "unused.db")  # отпустить файл до очистки
        self._tmp.cleanup()

    def test_risk_kill_switch_calls_all_subscribers(self):
        engine_like = _Subscriber(cancelled=["e1"])  # владелец 1 (как engine)
        router_like = _Subscriber(cancelled=["r1"])  # владелец 2 (как OrderRouter)
        register_kill_callback(engine_like)
        register_kill_callback(router_like)
        risk.set_order_canceller(kill_switch_dispatch)

        report = risk.kill_switch(flatten=False, by="test")

        self.assertEqual(engine_like.calls, [False])
        self.assertEqual(router_like.calls, [False])
        self.assertEqual(report["cancelled"], ["e1", "r1"])
        self.assertEqual(report["failed"], [])
        risk.reset_breaker("kill", by="test")


class OrderRouterKillWiringTest(_RegistryCase):
    """OrderRouter: подписка при __init__, отписка в close()."""

    def setUp(self):
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        # роутер при __init__ регистрирует диспетчер в risk — уводим
        # синглтон risk на temp-БД, чтобы не трогать data/risk_state.db
        risk.init(Path(self._tmp.name) / "risk.db")

    def tearDown(self):
        super().tearDown()
        risk.init(Path(self._tmp.name) / "unused.db")
        self._tmp.cleanup()

    def _registered_owners(self) -> set:
        return {getattr(cb, "__self__", None) for cb in registered_kill_callbacks()}

    def test_router_registers_on_init_and_unregisters_on_close(self):
        router = OrderRouter(object(), db_path=Path(self._tmp.name) / "state.db")
        self.assertIn(router, self._registered_owners())
        router.close()
        self.assertNotIn(router, self._registered_owners())

    def test_two_routers_both_registered(self):
        # одновременная работа двух владельцев: ни один колбэк не теряется
        r1 = OrderRouter(object(), db_path=Path(self._tmp.name) / "s1.db")
        r2 = OrderRouter(object(), db_path=Path(self._tmp.name) / "s2.db")
        owners = self._registered_owners()
        self.assertIn(r1, owners)
        self.assertIn(r2, owners)
        r1.close()
        r2.close()


if __name__ == "__main__":
    unittest.main()
