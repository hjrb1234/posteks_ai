import React from 'react';
import ReactDOM from 'react-dom/client';
import App from '../src/App';
import '../src/styles.css';
import { installPreviewTransport } from './transport';

// This entry is not referenced by the production index.html or production bundle.
document.documentElement.dataset.preview = 'true';
installPreviewTransport();
ReactDOM.createRoot(document.getElementById('root')!).render(<React.StrictMode><App/></React.StrictMode>);
