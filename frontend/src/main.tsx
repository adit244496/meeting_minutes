import React from "react";
import ReactDOM from "react-dom/client";
import { HashRouter, Navigate, Route, Routes } from "react-router-dom";

import App from "./App";
import Login from "./pages/Login";
import Meetings from "./pages/Meetings";
import MeetingDetail from "./pages/MeetingDetail";
import Admin from "./pages/Admin";
import { initTheme } from "./lib/theme";
import "./index.css";

initTheme();

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <HashRouter>
      <Routes>
        <Route path="/login" element={<Login />} />
        <Route path="/" element={<App />}>
          <Route index element={<Navigate to="/meetings" replace />} />
          <Route path="meetings" element={<Meetings />} />
          <Route path="meetings/:id" element={<MeetingDetail />} />
          <Route path="admin" element={<Admin />} />
        </Route>
      </Routes>
    </HashRouter>
  </React.StrictMode>,
);
