import '@telegram-apps/telegram-ui/dist/styles.css';
import { StrictMode } from 'react';
import ReactDOM from 'react-dom/client';

import App from './App';
import { initTelegram } from './telegram';
import './styles.css';

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
