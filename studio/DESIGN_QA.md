# Studio Design QA

Перед merge UI-изменения проверить не только happy path, но и реальные async/error состояния.

- [ ] Loading: initial data load визуально отличается от empty; layout зарезервирован skeleton/reserved geometry.
- [ ] Empty: empty copy показывается только после завершённой загрузки.
- [ ] Success: завершение действия понятно рядом с контекстом действия; background status использует `role="status"` / `aria-live="polite"`.
- [ ] Error: ошибка остаётся заметной, actionable и использует `role="alert"`.
- [ ] Disabled: блокируются только конфликтующие controls; независимые действия остаются доступны.
- [ ] Slow network: через несколько секунд всё ещё понятно, что именно выполняется; нет ложных `0` / `—` до первого ответа.
- [ ] Mobile / tablet / desktop: skeleton и длинные статусы не ломают layout на существующих breakpoints.
- [ ] Keyboard / focus: все actions доступны с клавиатуры; focus не теряется после async completion/error.
- [ ] Reduced motion: при `prefers-reduced-motion: reduce` декоративные animation/transition не обязательны для понимания состояния.
- [ ] Long Russian copy / channel names: текст переносится или обрезается предсказуемо без перекрытия controls.

Для каждого найденного gap добавлять конкретный regression/acceptance criterion; не маскировать state ambiguity декоративной анимацией.
