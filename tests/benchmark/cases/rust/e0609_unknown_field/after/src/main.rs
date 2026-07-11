pub struct Player { pub name: String, pub attempt: u32 }
fn main() {
    let p = Player { name: "Alex".into(), attempt: 0 };
    println!("{} ({})", p.name, p.attempt);
}
