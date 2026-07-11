struct Config { host: String, port: u16 }

fn main() {
    let c = Config { host: "localhost".into(), port: 8080 };
    println!("{}:{}", c.host, c.port);
}
