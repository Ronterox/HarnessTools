#include "event.h"

#include <stdlib.h>
#include <string.h>

#include "util.h"

static const char *KIND_NAMES[EV_KIND_COUNT] = {
    "turn", "read", "search", "create", "edit", "delete", "touch", "bash", "tool",
};

const char *ev_kind_name(EvKind k) { return k < EV_KIND_COUNT ? KIND_NAMES[k] : "?"; }

void ev_free(Event *e) {
    free(e->actor);
    free(e->actor_name);
    free(e->path);
    free(e->text);
    free(e->tool_id);
    memset(e, 0, sizeof *e);
}

int ev_cmp(const void *pa, const void *pb) {
    const Event *a = pa, *b = pb;
    if (a->ts != b->ts) return a->ts < b->ts ? -1 : 1;
    return a->seq < b->seq ? -1 : a->seq > b->seq;
}

void ev_print_json(FILE *f, const Event *e) {
    fprintf(f, "{\"ts\":%.3f,\"kind\":\"%s\",\"actor\":", e->ts, ev_kind_name(e->kind));
    json_str(f, e->actor);
    if (e->actor_name) { fputs(",\"name\":", f); json_str(f, e->actor_name); }
    if (e->path) { fputs(",\"path\":", f); json_str(f, e->path); }
    if (e->add || e->del) fprintf(f, ",\"add\":%d,\"del\":%d", e->add, e->del);
    if (e->lines >= 0) fprintf(f, ",\"lines\":%d", e->lines);
    fprintf(f, ",\"turn\":%d", e->turn);
    if (e->text) { fputs(",\"text\":", f); json_str(f, e->text); }
    fputs("}\n", f);
}

void evq_init(EvQueue *q) {
    memset(q, 0, sizeof *q);
    pthread_mutex_init(&q->mu, NULL);
}

void evq_push(EvQueue *q, Event e) {
    pthread_mutex_lock(&q->mu);
    if (q->n == q->cap) {
        q->cap = q->cap ? q->cap * 2 : 256;
        q->items = xrealloc(q->items, q->cap * sizeof *q->items);
    }
    q->items[q->n++] = e;
    pthread_mutex_unlock(&q->mu);
}

size_t evq_drain(EvQueue *q, Event **out) {
    pthread_mutex_lock(&q->mu);
    size_t n = q->n;
    *out = q->items;
    q->items = NULL;
    q->n = q->cap = 0;
    pthread_mutex_unlock(&q->mu);
    return n;
}
