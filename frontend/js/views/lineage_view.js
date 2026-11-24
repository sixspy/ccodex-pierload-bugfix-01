export const lineageViewTemplate = `
<section class="panel">
  <h2>轴重血缘</h2>
  <p class="hint">记录驳载分票、二次过磅等轴重血缘边；续磅票缺边即血统不齐。</p>
  <div class="detail-box">
    <p :class="lineageHealth.complete ? 'ok' : 'bad'">
      血统 {{ lineageHealth.complete ? '齐全' : '缺边待补' }}
      · 缺边票 {{ lineageHealth.missing_count }}
    </p>
    <ul v-if="lineageHealth.missing.length">
      <li v-for="m in lineageHealth.missing" :key="m.id">
        {{ m.ticket_code }} · {{ m.vehicle_plate }} · {{ m.bl_ref }} · {{ m.status }}
      </li>
    </ul>
  </div>
  <div class="form-row">
    <div><label>起点票</label>
      <select v-model.number="lineageForm.from_ticket_id">
        <option v-for="t in tickets" :key="t.id" :value="t.id">{{ t.ticket_code }}</option>
      </select>
    </div>
    <div><label>终点票(可空)</label>
      <select v-model.number="lineageForm.to_ticket_id">
        <option :value="null">—</option>
        <option v-for="t in tickets" :key="t.id" :value="t.id">{{ t.ticket_code }}</option>
      </select>
    </div>
    <div><label>类型</label><input v-model="lineageForm.edge_type"/></div>
    <div><label>说明</label><input v-model="lineageForm.detail"/></div>
  </div>
  <div class="actions">
    <button @click="addLineage">登记边</button>
    <button class="secondary" @click="refresh">刷新</button>
  </div>
  <table class="data-table">
    <thead><tr><th>自</th><th>至</th><th>类型</th><th>说明</th></tr></thead>
    <tbody>
      <tr v-for="e in lineageEdges" :key="e.id">
        <td>{{ e.from_code || e.from_ticket_id }}</td>
        <td>{{ e.to_code || '—' }}</td>
        <td>{{ e.edge_type }}</td>
        <td>{{ e.detail }}</td>
      </tr>
    </tbody>
  </table>
</section>
`;
