#ifndef AIG_MODEL_H
#define AIG_MODEL_H
#include <stdbool.h>

#include "event.h"
#include "raylib.h"
#include "util.h"

/* Scene state built from Events: the folder tree, actors, transient effects
 * and running totals. model_update() runs the layout physics. */

typedef struct {
    char *name;   /* last path component */
    char *path;   /* repo-relative */
    int parent;   /* node index, -1 for root */
    bool is_dir;
    bool deleted; /* fading out */
    bool attached; /* files: listed in the parent's layout slots */
    Vector2 pos, vel;
    float radius;     /* files: drawn size; dirs: extent of their files */
    float size;       /* dirs: drawn size, the average of their children's */
    float kids_sum;   /* dirs: scratch for that average */
    int kids_n;
    int lines;        /* -1 = unknown */
    float disp_lines; /* eased toward lines */
    Color color;      /* dirs only; files are drawn in their heat color */
    EvKind act_kind;  /* heat hue: last change's kind, EV_READ when only read */
    int churn;        /* heat brightness: times changed */
    float flash;      /* 1 on touch, decays */
    float label;      /* real seconds its name stays shown after a touch */
    int counted;      /* lines it adds to the average file size, -1 = none */
    float alpha;      /* fade-in / fade-out */
    double last_touch;
    EvKind last_kind; /* what last happened to it, by whom */
    int last_actor, last_add, last_del;
    int add, del;     /* session totals */
    int *kids, nkids, capkids; /* dirs: child files (layout slots) */
    int ndirs;        /* dirs: child dir count */
} Node;

typedef struct {
    char *id, *name;
    Color color;
    Vector2 pos, vel;
    int target;       /* node it heads to */
    double last_active;
    int turn;
    char *prompt;     /* current turn's prompt */
    char *bubble;     /* transient text (command, pattern) */
    float bubble_t;
    int add, del, files_created, files_edited, files_deleted, reads;
} Actor;

/* A touched file's name shows this long (real time, so it reads at any
 * replay speed), fading out over the last LABEL_FADE. */
#define LABEL_SECS 4.0f
#define LABEL_FADE 1.0f

typedef enum { FX_BEAM, FX_PULSE } FxKind;
/* How big a change is next to its file: a pulse's shape and brightness. */
typedef enum { RING_MINOR, RING_NOTABLE, RING_MAJOR } RingTier;
#define RING_NOTABLE_AT 0.15f /* of the file (or the average file) */
#define RING_MAJOR_AT 1.0f
#define RING_MAJOR_TOP 2.5f   /* a major ring's strength tops out here */
typedef struct {
    FxKind kind;
    EvKind ev;
    int actor, node;
    float strength; /* 0..1: beams from diff size, pulses within their tier */
    float age, life;
    Color color;
    RingTier tier;  /* pulses */
    float phase;    /* pulses: angle of a major ring's rays */
} Fx;

#define MAX_FX 1024

typedef struct {
    char *root;
    Node *nodes;
    int nnodes, capnodes;
    StrMap index; /* "d:path" / "f:path" -> node */
    Actor *actors;
    int nactors;
    Fx fx[MAX_FX];
    int fx_head, fx_count;
    long total_add, total_del;
    double sum_lines; /* over the files whose length is known: the average file */
    int nsized;
    int files_created, files_edited, files_deleted, reads, searches, commands;
} Scene;

Scene *model_new(const char *root);
void model_free(Scene *m);
int model_find(const Scene *m, bool is_dir, const char *path); /* -1 when absent */
/* Copy positions/sizes of nodes and actors that also exist in `old`, so a
 * rebuilt scene (timeline seek) keeps its layout instead of re-sprouting. */
void model_adopt_layout(Scene *m, const Scene *old);
void model_apply(Scene *m, const Event *e, bool effects);
void model_update(Scene *m, float dt, double now);
Color kind_color(EvKind k);

#endif
