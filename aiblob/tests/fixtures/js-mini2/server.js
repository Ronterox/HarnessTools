const express = require('express');
const notes = require('./notes');

const app = express();
const users = [];
const cache = Object.create(null);
const seed = ['ann'];
let hits = 0;
const LIMIT = 10;
users.push(...seed);

app.use('/notes', notes);

app.get(/^\/users\/(\d+)$/, (req, res) => {
  res.json(users[req.params[0]]);
});

app.get('/files/:name(\\w+)', (req, res) => {
  hits++;
  res.json(cache[req.params.name] || LIMIT);
});

app.post('/users', (req, res) => {
  users.push(req.body);
  res.status(201).json(users.length);
});

const verb = process.env.VERB || 'get';
app[verb]('/dynamic', (req, res) => res.end());

app.listen(3000);
