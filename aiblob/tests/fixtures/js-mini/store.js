const items = new Map();
let nextId = 1;

class Store {
  list() {
    return [...items.values()];
  }

  add(name) {
    if (this.list().some((i) => i.name === name)) {
      throw new Error(`duplicate: ${name}`);
    }
    const item = { id: nextId++, name };
    items.set(item.id, item);
    return item;
  }
}

module.exports = { Store };
