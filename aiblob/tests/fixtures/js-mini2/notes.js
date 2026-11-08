const router = require('express').Router();
const items = new Map();

function list(req, res) {
  res.json([...items.values()]);
}

function create(req, res) {
  items.set(req.body.id, req.body);
  res.sendStatus(201);
}

function audit(req, res, next) {
  next();
}

function show(req, res) {
  res.json(items.get(req.params.id));
}

router.route('/').get(list).post(create);
router.route('/:id').all(audit).get(show).delete((req, res) => {
  items.delete(req.params.id);
  res.sendStatus(204);
});

module.exports = router;
