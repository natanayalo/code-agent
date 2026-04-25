import React from 'react';
import { Header } from './components/Header';
import { TaskCard } from './components/TaskCard';
import { StatsPanel } from './components/StatsPanel';

function App() {
  return (
    <div className="dashboard-container">
      <Header />

      <main className="dashboard-main">
        <TaskCard
          status="Running"
          title="Implement PWA Frontend Architecture"
          description="Designing the core structure and selecting technology stack for the operator dashboard."
          commandsRun={3}
        />

        <StatsPanel
          completed={124}
          failed={2}
        />
      </main>
    </div>
  );
}

export default App;
