import numpy as np
import tensorflow as tf
from tensorflow.python.ops import tensor_array_ops, control_flow_ops


class OracleGru(object):
    def __init__(self, num_vocabulary, batch_size, emb_dim, hidden_dim, sequence_length, start_token, ):
        self.num_vocabulary = num_vocabulary
        self.batch_size = batch_size
        self.emb_dim = emb_dim
        self.hidden_dim = hidden_dim
        self.sequence_length = sequence_length
        self.start_token = tf.constant([start_token] * self.batch_size, dtype=tf.int32)
        self.g_params = []
        self.temperature = 1.0

        with tf.compat.v1.variable_scope('generator'):
            tf.compat.v1.set_random_seed(1234)
            self.g_embeddings = tf.Variable(
                tf.random.normal([self.num_vocabulary, self.emb_dim], 0.0, 1.0, seed=123314154))
            self.g_params.append(self.g_embeddings)
            self.g_recurrent_unit = self.create_recurrent_unit(self.g_params)  # maps h_tm1 to h_t for generator
            self.g_output_unit = self.create_output_unit(self.g_params)  # maps h_t to o_t (output token logits)

        # placeholder definition
        self.x = tf.compat.v1.placeholder(tf.int32, shape=[self.batch_size,
                                                 self.sequence_length])  # sequence of tokens generated by generator

        # processed for batch
        with tf.device("/cpu:0"):
            tf.compat.v1.set_random_seed(1234)
            self.processed_x = tf.transpose(a=tf.nn.embedding_lookup(params=self.g_embeddings, ids=self.x),
                                            perm=[1, 0, 2])  # seq_length x batch_size x emb_dim

        # initial states
        self.h0 = tf.zeros([self.batch_size, self.hidden_dim])
        self.h0 = tf.stack([self.h0, self.h0])

        # generator on initial randomness
        gen_o = tensor_array_ops.TensorArray(dtype=tf.float32, size=self.sequence_length,
                                             dynamic_size=False, infer_shape=True)
        gen_x = tensor_array_ops.TensorArray(dtype=tf.int32, size=self.sequence_length,
                                             dynamic_size=False, infer_shape=True)

        def _g_recurrence(i, x_t, h_tm1, gen_o, gen_x):
            h_t = self.g_recurrent_unit(x_t, h_tm1)  # hidden_memory_tuple
            o_t = self.g_output_unit(h_t)  # batch x vocab , logits not prob
            log_prob = tf.math.log(tf.nn.softmax(o_t))
            next_token = tf.cast(tf.reshape(tf.random.categorical(logits=log_prob, num_samples=1), [self.batch_size]), tf.int32)
            x_tp1 = tf.nn.embedding_lookup(params=self.g_embeddings, ids=next_token)  # batch x emb_dim
            gen_o = gen_o.write(i, tf.reduce_sum(
                input_tensor=tf.multiply(tf.one_hot(next_token, self.num_vocabulary, 1.0, 0.0), tf.nn.softmax(o_t)),
                axis=1))  # [batch_size] , prob
            gen_x = gen_x.write(i, next_token)  # indices, batch_size
            return i + 1, x_tp1, h_t, gen_o, gen_x

        _, _, _, self.gen_o, self.gen_x = control_flow_ops.while_loop(
            cond=lambda i, _1, _2, _3, _4: i < self.sequence_length,
            body=_g_recurrence,
            loop_vars=(tf.constant(0, dtype=tf.int32),
                       tf.nn.embedding_lookup(params=self.g_embeddings, ids=self.start_token), self.h0, gen_o, gen_x)
        )

        self.gen_x = self.gen_x.stack()  # seq_length x batch_size
        self.gen_x = tf.transpose(a=self.gen_x, perm=[1, 0])  # batch_size x seq_length

        # supervised pretraining for generator
        g_predictions = tensor_array_ops.TensorArray(
            dtype=tf.float32, size=self.sequence_length,
            dynamic_size=False, infer_shape=True)

        ta_emb_x = tensor_array_ops.TensorArray(
            dtype=tf.float32, size=self.sequence_length)
        ta_emb_x = ta_emb_x.unstack(self.processed_x)

        def _pretrain_recurrence(i, x_t, h_tm1, g_predictions):
            h_t = self.g_recurrent_unit(x_t, h_tm1)
            o_t = self.g_output_unit(h_t)
            g_predictions = g_predictions.write(i, tf.nn.softmax(o_t))  # batch x vocab_size
            x_tp1 = ta_emb_x.read(i)
            return i + 1, x_tp1, h_t, g_predictions

        _, _, _, self.g_predictions = control_flow_ops.while_loop(
            cond=lambda i, _1, _2, _3: i < self.sequence_length,
            body=_pretrain_recurrence,
            loop_vars=(tf.constant(0, dtype=tf.int32),
                       tf.nn.embedding_lookup(params=self.g_embeddings, ids=self.start_token),
                       self.h0, g_predictions))

        self.g_predictions = tf.transpose(
            a=self.g_predictions.stack(), perm=[1, 0, 2])  # batch_size x seq_length x vocab_size

        # pretraining loss
        self.pretrain_loss = -tf.reduce_sum(
            input_tensor=tf.one_hot(tf.cast(tf.reshape(self.x, [-1]), dtype=tf.int32), self.num_vocabulary, 1.0, 0.0) * tf.math.log(
                tf.reshape(self.g_predictions, [-1, self.num_vocabulary]))) / (self.sequence_length * self.batch_size)

        self.out_loss = tf.reduce_sum(
            input_tensor=tf.reshape(
                -tf.reduce_sum(
                    input_tensor=tf.one_hot(tf.cast(tf.reshape(self.x, [-1]), dtype=tf.int32), self.num_vocabulary, 1.0, 0.0) * tf.math.log(
                        tf.reshape(self.g_predictions, [-1, self.num_vocabulary])), axis=1
                ), [-1, self.sequence_length]
            ), axis=1
        )  # batch_size


        # Compute the similarity between minibatch examples and all embeddings.
        # We use the cosine distance:

    def set_similarity(self, valid_examples=None, pca=True):
        if valid_examples == None:
            if pca:
                valid_examples = np.array(range(20))
            else:
                valid_examples = np.array(range(self.num_vocabulary))
        self.valid_dataset = tf.constant(valid_examples, dtype=tf.int32)
        self.norm = tf.sqrt(tf.reduce_sum(input_tensor=tf.square(self.g_embeddings), axis=1, keepdims=True))
        self.normalized_embeddings = self.g_embeddings / self.norm
        # PCA
        if self.num_vocabulary >= 20 and pca == True:
            emb = tf.matmul(self.normalized_embeddings, tf.transpose(a=self.normalized_embeddings))
            s, u, v = tf.linalg.svd(emb)
            u_r = tf.strided_slice(u, begin=[0, 0], end=[20, self.num_vocabulary], strides=[1, 1])
            self.normalized_embeddings = tf.matmul(u_r, self.normalized_embeddings)
        self.valid_embeddings = tf.nn.embedding_lookup(
            params=self.normalized_embeddings, ids=self.valid_dataset)
        self.similarity = tf.matmul(self.valid_embeddings, tf.transpose(a=self.normalized_embeddings))

    def generate(self, session):
        # h0 = np.random.normal(size=self.hidden_dim)
        outputs = session.run(self.gen_x)
        return outputs

    def init_matrix(self, shape):
        return tf.random.normal(shape, stddev=1.0, seed=10)

    def create_recurrent_unit(self, params):
        # Weights and Bias for input and hidden tensor
        self.Wf = tf.Variable(tf.random.normal([self.emb_dim, self.hidden_dim], 0.0, 1000000.0, seed=111))
        self.bf = tf.Variable(tf.random.normal([self.hidden_dim, ], 0.0, 1000000.0, seed=311))

        self.Wr = tf.Variable(tf.random.normal([self.emb_dim, self.hidden_dim], 0.0, 1000000.0, seed=112))
        self.br = tf.Variable(tf.random.normal([self.hidden_dim, ], 0.0, 1000000.0, seed=312))

        self.W = tf.Variable(tf.random.normal([self.emb_dim, self.hidden_dim], 0.0, 1000000.0, seed=113))

        params.extend([
            self.Wf, self.bf,
            self.Wr, self.br,
            self.W,
        ])

        def sru_unit(x, hidden_memory_stack):
            _, previous_c = tf.unstack(hidden_memory_stack)

            # transformation
            xt_ = tf.matmul(x, self.W)

            # forget gate
            ft = tf.sigmoid(
                tf.matmul(x, self.Wf) +
                self.bf
            )
            # reset gate
            rt = tf.sigmoid(
                tf.matmul(x, self.Wr) +
                self.br
            )
            # internal state
            ct = tf.multiply(ft, previous_c) + tf.multiply(tf.subtract(1.0, ft), xt_)

            # new memory
            ht = tf.multiply(rt, tf.tanh(ct)) + tf.multiply(tf.subtract(1.0, rt), x)
            # current_hidden_state = tf.multiply(tf.subtract(1.0, Zt), ht_) + tf.multiply(Zt, previous_hidden_state)
            return tf.stack([ht, ct])

        return sru_unit

    def create_output_unit(self, params):
        self.Wo = tf.Variable(tf.random.normal([self.hidden_dim, self.num_vocabulary], 0.0, 1.0, seed=12341))
        self.bo = tf.Variable(tf.random.normal([self.num_vocabulary], 0.0, 1.0, seed=56865246))
        params.extend([self.Wo, self.bo])

        def unit(hidden_memory_tuple):
            hidden_state, _ = tf.unstack(hidden_memory_tuple)
            logits = tf.matmul(hidden_state, self.Wo) + self.bo
            return logits

        return unit
