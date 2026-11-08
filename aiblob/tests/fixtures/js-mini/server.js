const express = require('express');
const { Store } = require('./store');
const { isValidName, logError } = require('./util');

const app = express();
const store = new Store();

app.use(express.json());
app.use(express.static('public'));

app.get('/items', (req, res) => {
  res.json(store.list());
});

app.post('/items', (req, res, next) => {
  const names = [req.body.name];
  if (!names.every(isValidName)) {
    return res.status(400).json({ error: 'invalid name' });
  }
  try {
    res.status(201).json(store.add(req.body.name));
  } catch (err) {
    logError(err);
    next(err);
  }
});

app.use((err, req, res, next) => {
  logError(err);
  res.status(500).json({ error: err.message });
});

app.listen(process.env.PORT || 3000);
