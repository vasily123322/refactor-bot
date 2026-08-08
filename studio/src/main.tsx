import '@telegram-apps/telegram-ui/dist/styles.css';
import { StrictMode } from 'react';
import ReactDOM from 'react-dom/client';

import App from './App';
import './rich.css';
import './source-settings.css';
import './sources.css';
import './styles.css';
import { initTelegram } from './telegram';

const root = ReactDOM.createRoot(document.getElementById('root')!);

void initTelegram()
  .catch(() => {
    // API auth will show a useful message when Studio is opened outside Telegram.
  })
  .finally(() => {
    root.render(
      <StrictMode>
        <App />
      </StrictMode>,
    );
  });
