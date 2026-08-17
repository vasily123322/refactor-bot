import './telegram-delivery-settings.css';

import {
  patchTelegramDeliverySettings,
  telegramDeliverySettings,
} from './telegramDeliverySettings';
import type { PostDocument } from './types';

export function TelegramDeliverySettings({
  document,
  onChange,
}: {
  document: PostDocument;
  onChange: (document: PostDocument) => void;
}) {
  const settings = telegramDeliverySettings(document);
  return (
    <section className="telegram-delivery-settings" aria-label="Настройки доставки Telegram">
      <div>
        <strong>Доставка в Telegram</strong>
        <small>Применяется одинаково к preview и production renderer.</small>
      </div>
      <label className="rich-check-row">
        <input
          type="checkbox"
          checked={settings.silent}
          onChange={(event) => onChange(
            patchTelegramDeliverySettings(document, { silent: event.target.checked }),
          )}
        />
        <span>Без уведомления</span>
      </label>
      <label className="rich-check-row">
        <input
          type="checkbox"
          checked={settings.protectContent}
          onChange={(event) => onChange(
            patchTelegramDeliverySettings(document, { protectContent: event.target.checked }),
          )}
        />
        <span>Защитить от пересылки / сохранения</span>
      </label>
    </section>
  );
}
