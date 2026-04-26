import React from 'react';
import { DashboardLayout } from './components/layout/DashboardLayout';
import { TaskBoard } from './components/TaskBoard';
import { StatsPanel } from './components/StatsPanel';

function App() {
  return (
    <DashboardLayout>
      <div className="dashboard-summary">
        <StatsPanel
          completed={124}
          failed={2}
        />
      </div>

      <TaskBoard />
    </DashboardLayout>
  );
}

export default App;
